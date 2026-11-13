"""GitLab webhooks of the panel.

Registering is asked by the SPA after GitLab lines are saved, or by a
button: never inside the ORM's ``write``, so a save makes no network
call. «Verify» asks GitLab for a test delivery and returns at once with
``verify_requested_at``; the SPA polls ``get_git_hooks`` until the last
delivery is newer: no worker waits, and nothing
reads another request's insert through a REPEATABLE READ snapshot.

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""
import logging

from odoo import _, api, fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ...gitlab.client import GitLabError
from ...models._concurrency import read_committed_cursor

_logger = logging.getLogger(__name__)

#: The repository-line models a webhook can be registered from.
_LINE_MODELS = {'project': 'cloud.project.repo', 'instance': 'cloud.instance.repo'}

#: How many failed deliveries of a webhook the SPA lists, newest first.
_FAILED_EVENTS_SHOWN = 5


def _failed_events(hook):
    """Return the newest deliveries of *hook* whose dispatch failed.

    They are the ones a manager may reprocess (``/cloud/reprocess_git_event``).

    :param hook: a ``cloud.gitlab.hook`` (sudo)
    :return: ``[{id, event_type, error, attempts, created_at}]``
    :rtype: list
    """
    events = hook.env['cloud.gitlab.event'].sudo().search([
        ('hook_id', '=', hook.id), ('processed', '=', False), ('error', '!=', False),
    ], order='id desc', limit=_FAILED_EVENTS_SHOWN)
    return [{
        'id': ev.id,
        'event_type': ev.event_type or '',
        'error': ev.error or '',
        'attempts': ev.dispatch_attempts,
        'created_at': fields.Datetime.to_string(ev.create_date),
    } for ev in events]


def _serialize_hook(hook, with_secret_state):
    """Return what the SPA shows of a webhook.

    :param hook: a ``cloud.gitlab.hook`` (sudo)
    :param bool with_secret_state: whether the viewer is a manager
    :rtype: dict
    """
    last = hook._last_delivery_at()
    return {
        'id': hook.id,
        'provider_id': hook.provider_id.id,
        'provider': hook.provider_id.name,
        'scope': hook.scope,
        'path': hook.path,
        'state': hook.state,
        'by_api': bool(hook.remote_hook_id),
        'last_delivery_at': fields.Datetime.to_string(last) if last else False,
        'verify_requested_at': fields.Datetime.to_string(hook.verify_requested_at)
        if hook.verify_requested_at else False,
        'url': hook._url() if with_secret_state else False,
        'failed_events': _failed_events(hook) if with_secret_state else [],
    }


def _readable_by(hook, env):
    """Return whether the user of *env* may read a repository line *hook* serves.

    :param hook: a ``cloud.gitlab.hook`` (sudo)
    :param env: the environment of the user who asks
    :rtype: bool
    """
    return any(
        lines.with_env(env)._filtered_access('read') for lines in hook._lines()
    )


class GitHooksMixin:

    def _hook_or_error(self, hook_id):
        """Return the webhook *hook_id* (sudo), or None.

        :rtype: cloud.gitlab.hook or None
        """
        hook = request.env['cloud.gitlab.hook'].sudo().browse(int(hook_id)).exists()
        return hook or None

    def _check_can_edit_hook(self, hook):
        """Refuse unless the user may edit a project one of the hook's lines belongs to.

        A manager always may.

        :raises AccessError: otherwise
        """
        sec = self._sec()
        sec._check_cloud_group('group_cloud_consultant')
        if sec._has_cloud_group('group_cloud_manager'):
            return
        instance_lines, project_lines = hook._lines()
        owners = instance_lines.instance_id.project_id | project_lines.project_id
        user_env = request.env
        for project in owners:
            try:
                project.with_env(user_env).check_access('write')
                return
            except AccessError:
                continue
        raise AccessError(_("You cannot edit a project this webhook serves."))

    @http.route(['/cloud/get_git_hooks'], type='json', auth='user')
    def cloud_get_git_hooks(self):
        """Return the panel's GitLab webhooks, their state and last delivery.

        :return: ``{ok, can_manage, hooks}``
        :rtype: dict
        """
        sec = self._sec()
        sec._check_cloud_group('group_cloud_consultant')
        can_manage = sec._has_cloud_group('group_cloud_manager')
        hooks = request.env['cloud.gitlab.hook'].sudo().search([])
        if not can_manage:
            # The paths of the lines the user may read, no other.
            hooks = hooks.filtered(lambda h: _readable_by(h, request.env))
        return {
            'ok': True, 'can_manage': can_manage,
            'hooks': [_serialize_hook(h, can_manage) for h in hooks],
        }

    @http.route(['/cloud/register_git_hook'], type='json', auth='user')
    def cloud_register_git_hook(self, line_model, line_id, scope='project'):
        """Create or reuse the webhook of a GitLab line's path, and register it.

        ``scope='group'`` (managers only) covers the line's group.
        Idempotent, on a READ COMMITTED cursor of its own committed at
        the end: ``INSERT … ON CONFLICT DO NOTHING``, then the row held
        ``FOR NO KEY UPDATE`` while GitLab is called. A second request at
        once waits for the first and reads its ``remote_hook_id``, so it
        adopts the webhook instead of creating another. Before, with the
        row there and no ``remote_hook_id`` yet, two requests each found
        no webhook of the panel in GitLab and created one: GitLab
        delivered every push twice, and a serialization failure did not
        stop it, since Odoo's HTTP layer retries those.

        :param str line_model: ``project`` or ``instance``
        :param int line_id: the repository line
        :param str scope: ``project`` or ``group``
        :return: ``{ok, hook, manual, reason}``
        :rtype: dict
        """
        sec = self._sec()
        sec._check_cloud_group('group_cloud_consultant')
        model = _LINE_MODELS.get(line_model)
        if not model or scope not in ('project', 'group'):
            return {'ok': False, 'error': _("Unknown repository line.")}
        line = request.env[model].browse(int(line_id)).exists()
        if not line:
            return {'ok': False, 'error': _("Repository line not found.")}
        line.check_access('write')
        line = line.sudo()
        if not line.provider_id or not line.repo_path:
            return {'ok': False, 'error': _("Only GitLab lines take a webhook.")}
        path = line.repo_path
        if scope == 'group':
            sec._check_cloud_group('group_cloud_manager')
            path = path.rsplit('/', 1)[0]
        can_manage = sec._has_cloud_group('group_cloud_manager')
        provider_id = line.provider_id.id
        with read_committed_cursor(request.env.registry) as cr:
            env = api.Environment(cr, request.env.uid, dict(request.env.context))
            hook = self._ensure_hook(env, provider_id, scope, path)
            reason = hook._register_remote()
            answer = {
                'ok': True, 'hook': _serialize_hook(hook, can_manage),
                'manual': bool(reason), 'reason': reason or '',
            }
            cr.commit()
        return answer

    @staticmethod
    def _ensure_hook(env, provider_id, scope, path):
        """Return the webhook of ``(provider, scope, path)``, created if missing, locked.

        Two users pressing at once, or a retry, meet the same row: the
        second waits on the lock until the first commits, then reads
        what it wrote. Run it on a READ COMMITTED cursor, where the
        conflict and the re-read see a row committed by another
        request after this one began.

        :param env: an environment on a READ COMMITTED cursor
        :param int provider_id: the ``cloud.git.provider``
        :param str scope: ``project`` or ``group``
        :param str path: the project or group path
        :rtype: cloud.gitlab.hook (sudo)
        """
        Hook = env['cloud.gitlab.hook'].sudo()
        cr = env.cr
        defaults = Hook.default_get(['hook_key', 'secret'])
        cr.execute("""
            INSERT INTO cloud_gitlab_hook
                (provider_id, scope, path, hook_key, state,
                 create_uid, create_date, write_uid, write_date)
            VALUES (%s, %s, %s, %s, 'pending', %s, now() at time zone 'utc',
                    %s, now() at time zone 'utc')
            ON CONFLICT (provider_id, scope, path) DO NOTHING
            RETURNING id
        """, (provider_id, scope, path, defaults['hook_key'], env.uid, env.uid))
        row = cr.fetchone()
        if row:
            hook = Hook.browse(row[0])
            hook.secret = defaults['secret']
            return hook
        # Held while GitLab is called; FOR NO KEY UPDATE, as the row lock
        # rule asks: no column of a unique index changes.
        cr.execute("""
            SELECT id FROM cloud_gitlab_hook
             WHERE provider_id = %s AND scope = %s AND path = %s
               FOR NO KEY UPDATE
        """, (provider_id, scope, path))
        return Hook.browse(cr.fetchone()[0])

    @http.route(['/cloud/reveal_git_hook_secret'], type='json', auth='user')
    def cloud_reveal_git_hook_secret(self, hook_id, rotate=False):
        """Return a webhook's URL and secret to paste in GitLab; audited.

        :param bool rotate: give it a new secret first (and GitLab too when
            the webhook is the panel's)
        :return: ``{ok, url, secret}``
        :rtype: dict
        """
        self._sec()._check_cloud_group('group_cloud_manager')
        hook = self._hook_or_error(hook_id)
        if not hook:
            return {'ok': False, 'error': _("Webhook not found.")}
        secret = hook.secret
        if rotate:
            try:
                # Committed on a cursor of its own: this request's snapshot
                # still reads the old secret.
                secret = hook._rotate_secret()
            except UserError as exc:
                return {'ok': False, 'error': str(exc.args[0])}
        request.env['cloud.audit.log'].sudo().create({
            'action': 'GitLab webhook secret rotated' if rotate else 'GitLab webhook secret shown',
            'details': f"{hook.provider_id.name}: {hook.path}"[:255],
        })
        return {'ok': True, 'url': hook._url(), 'secret': secret}

    @http.route(['/cloud/verify_git_hook'], type='json', auth='user')
    def cloud_verify_git_hook(self, hook_id):
        """Ask GitLab for a test delivery and return without waiting for it.

        :return: ``{ok, verify_requested_at}`` or ``{ok: False, error}``
        :rtype: dict
        """
        hook = self._hook_or_error(hook_id)
        if not hook:
            return {'ok': False, 'error': _("Webhook not found.")}
        self._check_can_edit_hook(hook)
        return hook._request_test()

    @http.route(['/cloud/delete_git_hook'], type='json', auth='user')
    def cloud_delete_git_hook(self, hook_id):
        """Delete a webhook in GitLab (when it is the panel's) and in the panel.

        Without a credential that may delete it in GitLab, or with one
        GitLab refuses (401, 403), it is deleted in the panel only, and
        ``warning`` says it is left in GitLab.

        :return: ``{ok, warning}`` or ``{ok: False, error}``
        :rtype: dict
        """
        self._sec()._check_cloud_group('group_cloud_manager')
        hook = self._hook_or_error(hook_id)
        if not hook:
            return {'ok': False, 'error': _("Webhook not found.")}
        reason = ''
        if hook.remote_hook_id:
            try:
                client = hook._client('api_write')
            except UserError as exc:
                # No credential may delete it (or the server is refused):
                # the panel forgets it, and says that GitLab keeps it.
                client = None
                reason = exc.args[0]
            if client:
                try:
                    client.delete_hook(
                        hook._kind(), hook.gitlab_id or hook.path, hook.remote_hook_id,
                    )
                except GitLabError as exc:
                    if exc.code in ('auth', 'forbidden'):
                        # A credential GitLab refuses is as good as none.
                        reason = _(
                            "GitLab refused the credential: %(status)s",
                            status=exc.status or exc.code,
                        )
                    elif exc.code != 'not_found':
                        return {'ok': False, 'error': _(
                            "GitLab did not delete the webhook: delete it there "
                            "by hand (Settings → Webhooks), then here."
                        )}
        warning = _(
            "The panel cannot delete the webhook in GitLab (%(reason)s): "
            "it is deleted here only. Delete it in GitLab by hand "
            "(Settings → Webhooks).", reason=reason,
        ) if reason else ''
        hook.unlink()
        return {'ok': True, 'warning': warning}

    @http.route(['/cloud/reprocess_git_event'], type='json', auth='user')
    def cloud_reprocess_git_event(self, event_id):
        """Dispatch again a GitLab delivery whose dispatch failed.

        :return: ``{ok, processed, error}``
        :rtype: dict
        """
        self._sec()._check_cloud_group('group_cloud_manager')
        event = request.env['cloud.gitlab.event'].sudo().browse(int(event_id)).exists()
        if not event or event.processed:
            return {'ok': False, 'error': _("Nothing to reprocess.")}
        event._dispatch_safely()
        return {'ok': True, 'processed': event.processed, 'error': event.error or ''}

