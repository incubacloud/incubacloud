"""Settings → Git providers: GitLab servers and their credentials.

A manager adds a server (its base URL and, for a private CA, the CA
bundle) and its credentials, each a token scoped to a namespace. The
token is write-only: no route returns it, and an empty one on save keeps
the stored token. Saving a new token, and «Test connection», read from
GitLab what the token is (scopes, expiry, user); the test also asks the
repository's heads with git, so each channel answers with its own code
(plan-gitlab.md §4.5, §4.11, 8-nov-2026).

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""
import logging

from psycopg2 import IntegrityError

from odoo import _, fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from ...gitlab.client import GitLabError
from ...models._git_urls import parse_provider_repo_url
from ...models._repo_requirements import consume_git_read
from ...net.git_reader import GitReaderError
from ...net.outbound import OutboundError
from .._safe_error import safe_error_response

_logger = logging.getLogger(__name__)

#: The fields of a credential the SPA may write.
_CREDENTIAL_FIELDS = ('namespace', 'kind', 'username', 'expires_on')


def git_error_message(code):
    """Return what a read or API failure *code* means, translated.

    The codes of ``net.git_reader.GitReaderError`` and
    ``gitlab.client.GitLabError``, and the route's own ``no_api`` and
    ``no_credential``. Nothing in the text comes from the remote.

    :param str code: the failure code
    :rtype: str
    """
    messages = {
        'ok': _("It works."),
        'auth': _(
            "The server refused the credential: check the token and its "
            "scopes under Settings → Git providers."
        ),
        'forbidden': _(
            "The token lacks the scope or role this needs: check it under "
            "Settings → Git providers."
        ),
        'tls': _(
            "The server's certificate is not trusted: add the CA of the "
            "server under Settings → Git providers."
        ),
        'not_found': _(
            "The repository or branch was not found, or the credential "
            "cannot see it."
        ),
        'unreachable': _("The panel cannot reach the Git server."),
        'private_address': _(
            "This panel connects only to Git servers on a public address."
        ),
        'timeout': _("The Git server took too long to answer."),
        'too_large': _("The repository is too large to read from the panel."),
        'redirect': _(
            "The Git server answered with a redirect, which the panel does "
            "not follow: check that the repository has not moved."
        ),
        'git_too_old': _(
            "The panel's git is too old to pin the server's address "
            "(2.37 or later is needed)."
        ),
        'rate_limited': _("The Git server asks to wait: try again in a minute."),
        'no_api': _(
            "The token does not answer on the API (a deploy token): the "
            "panel reads the repository, nothing more."
        ),
        'no_credential': _(
            "No credential covers this repository: it is read as a public "
            "one."
        ),
    }
    return messages.get(code) or _("The Git server answered with an error.")


def _cause_code(exc):
    """Return the failure code a ``UserError`` of the introspection hides.

    :param UserError exc: what ``cloud.git.credential._introspect`` raised
    :rtype: str
    """
    cause = exc.__cause__
    if isinstance(cause, GitLabError):
        return cause.code
    if isinstance(cause, OutboundError):
        return 'private_address'
    return 'http'


def _constraint_message(env, exc):
    """Return the message of the provider's or credential's constraint *exc* broke.

    :param env: an Odoo environment whose cursor is usable again (the
        savepoint that failed was rolled back)
    :param IntegrityError exc: what saving a provider and its
        credentials raised
    :return: the constraint's message, translated, or Odoo's generic one
    :rtype: str
    """
    model = env['cloud.git.provider']
    if exc.diag.table_name == env['cloud.git.credential']._table:
        model = env['cloud.git.credential']
    return model._sql_error_to_message(exc)


def _serialize_provider(provider, with_credentials):
    """Return a provider for the SPA, its credentials without token.

    :param provider: a ``cloud.git.provider`` record
    :param bool with_credentials: whether to list the credentials (and
        the editable CA bundle), for a manager
    :rtype: dict
    """
    row = {
        'id': provider.id,
        'name': provider.name,
        'base_url': provider.base_url,
        'authority': provider.authority,
        'has_ca': bool(provider.ca_bundle),
        'ca_fingerprints': (provider.ca_fingerprints or '').split('\n')
        if provider.ca_fingerprints else [],
        'last_check_at': fields.Datetime.to_string(provider.last_check_at)
        if provider.last_check_at else False,
        'last_check_git': provider.last_check_git or '',
        'last_check_api': provider.last_check_api or '',
    }
    if with_credentials:
        row['ca_bundle'] = provider.ca_bundle or ''
        row['credentials'] = [{
            'id': cred.id,
            'namespace': cred.namespace or '',
            'kind': cred.kind,
            'username': cred.username or '',
            'scopes': cred.scopes or '',
            'token_user': cred.token_user or '',
            'expires_on': fields.Date.to_string(cred.expires_on)
            if cred.expires_on else False,
            'has_token': bool(cred.sudo().token),
            # A token that can write travels inside the image of 11.0 to
            # 19.0 when it is the one that clones: the tab says so, and
            # of one whose scopes were not read, that it is taken as such.
            'writes': cred._writes(),
            'scopes_unchecked': cred._scopes_unread(),
        } for cred in provider.credential_ids]
    return row


class GitProvidersMixin:
    """Routes of Settings → Git providers."""

    @http.route(['/cloud/get_git_providers'], type='jsonrpc', auth='user')
    def cloud_get_git_providers(self):
        """Return the panel's Git providers and where it may connect.

        A consultant sees the servers; a manager also their credentials,
        without the token.

        :return: ``{ok, network, can_manage, providers}``
        :rtype: dict
        """
        sec = self._sec()
        sec._check_cloud_group('group_cloud_consultant')
        can_manage = sec._has_cloud_group('group_cloud_manager')
        Provider = request.env['cloud.git.provider'].sudo()
        return {
            'ok': True,
            'network': Provider._network_policy(),
            'can_manage': can_manage,
            'providers': [
                _serialize_provider(p, can_manage) for p in Provider.search([])
            ],
        }

    @http.route(['/cloud/save_git_provider'], type='jsonrpc', auth='user')
    def cloud_save_git_provider(self, vals):
        """Add or edit a provider and its credentials, all or nothing.

        *vals*: ``id`` (to edit), ``name``, ``base_url``, ``ca_bundle``
        and ``credentials``, each with ``id`` (to edit), ``namespace``,
        ``kind``, ``username``, ``token`` and ``expires_on``. A credential
        of the provider left out of the list is removed; an empty token
        keeps the stored one. A new token that is not a deploy token is
        introspected; a failure there keeps what was saved and comes back
        as a warning.

        :param dict vals: the provider as the SPA edits it
        :return: ``{ok, provider, warnings}`` or ``{ok: False, error}``
        :rtype: dict
        """
        self._sec()._check_can_manage_settings()
        env = request.env
        to_introspect = env['cloud.git.credential']
        try:
            with env.cr.savepoint():
                provider = self._write_git_provider(vals or {})
                to_introspect = self._write_git_credentials(
                    provider, (vals or {}).get('credentials') or [],
                )
        except (UserError, ValidationError) as exc:
            return {'ok': False, 'error': str(exc)}
        except IntegrityError as exc:
            # A second provider of one server, or two credentials of one
            # namespace: the constraint's own message, not a reference.
            return {'ok': False, 'error': _constraint_message(env, exc)}
        except Exception as exc:
            return safe_error_response(exc, _("The Git provider could not be saved."))
        warnings = [
            _(
                "The token of the credential for %s must be entered again: "
                "a token is kept only for the server it was given for.",
                cred.namespace or provider.authority,
            )
            for cred in provider.credential_ids if not cred.sudo().token
        ]
        # Each introspection asks GitLab: one read of the hourly cap for
        # the save (security review of G1, 8-nov-2026).
        if to_introspect and not consume_git_read(env):
            warnings.append(_(
                "Git read limit reached: the new tokens were saved without "
                "asking GitLab what they are. Test the provider later."
            ))
            to_introspect = env['cloud.git.credential']
        for cred in to_introspect:
            try:
                cred._introspect()
            except UserError as exc:
                warnings.append(str(exc))
        return {
            'ok': True,
            'provider': _serialize_provider(provider, True),
            'warnings': warnings,
        }

    def _write_git_provider(self, vals):
        """Create or write the provider of *vals*.

        :param dict vals: the provider as the SPA edits it
        :rtype: cloud.git.provider
        :raises UserError: when the name is missing or the id unknown
        """
        Provider = request.env['cloud.git.provider']
        name = (vals.get('name') or '').strip()
        if not name:
            raise UserError(_("The Git provider needs a name."))
        if not (vals.get('base_url') or '').strip():
            raise UserError(_("The Git provider needs a base URL."))
        write = {
            'name': name,
            'base_url': (vals.get('base_url') or '').strip(),
            'ca_bundle': (vals.get('ca_bundle') or '').strip() or False,
        }
        if vals.get('id'):
            provider = Provider.browse(int(vals['id'])).exists()
            if not provider:
                raise UserError(_("Git provider not found."))
            if write['base_url'] == provider.base_url:
                del write['base_url']
            provider.write(write)
            return provider
        return Provider.create(write)

    def _write_git_credentials(self, provider, rows):
        """Make *provider*'s credentials those of *rows*.

        :param provider: the ``cloud.git.provider`` record
        :param list rows: the credentials as the SPA edits them
        :return: the credentials with a new token to introspect
        :rtype: cloud.git.credential
        :raises UserError: when a row names a credential of another provider
        """
        Credential = request.env['cloud.git.credential']
        kept = Credential
        fresh = Credential
        for row in rows:
            write = {k: row[k] for k in _CREDENTIAL_FIELDS if k in row}
            write['expires_on'] = write.get('expires_on') or False
            token = (row.get('token') or '').strip()
            if token:
                write['token'] = token
            if row.get('id'):
                cred = Credential.browse(int(row['id'])).exists()
                if cred.provider_id != provider:
                    raise UserError(_("Git credential not found."))
                cred.write(write)
            else:
                cred = Credential.create(write | {'provider_id': provider.id})
            if cred.kind == 'deploy':
                cred.sudo().write({'scopes': '', 'token_user': False})
            elif token:
                fresh |= cred
            kept |= cred
        (provider.credential_ids - kept).unlink()
        return fresh

    @http.route(['/cloud/delete_git_provider'], type='jsonrpc', auth='user')
    def cloud_delete_git_provider(self, provider_id):
        """Delete a provider no repository line uses.

        :param int provider_id: the provider
        :return: ``{ok}``, or ``{ok: False, error}`` saying how many
            lines still use it
        :rtype: dict
        """
        self._sec()._check_can_manage_settings()
        env = request.env
        provider = env['cloud.git.provider'].browse(int(provider_id)).exists()
        if not provider:
            return {'ok': False, 'error': _("Git provider not found.")}
        in_use = sum(
            env[model].sudo().with_context(active_test=False).search_count(
                [('provider_id', '=', provider.id)],
            )
            for model in ('cloud.project.repo', 'cloud.instance.repo')
        ) + env['cloud.gitlab.hook'].sudo().search_count(
            [('provider_id', '=', provider.id)],
        )
        if in_use:
            return {'ok': False, 'error': _(
                "%(count)s repository lines or webhooks use %(name)s: change "
                "or remove them first.",
                count=in_use, name=provider.name,
            )}
        provider.unlink()
        return {'ok': True}

    @http.route(['/cloud/test_git_provider'], type='jsonrpc', auth='user')
    def cloud_test_git_provider(self, provider_id, url):
        """Test a provider with a repository of it, on both channels.

        Git: the repository's ``HEAD`` with ``ls-remote`` and the
        credential git reads it with (none: as a public one). API: the
        introspection of that same credential (§4.3), so both channels
        speak of one token. Each answers with a code and its text; the
        codes are kept on the provider.

        :param int provider_id: the provider
        :param str url: a repository of the provider
        :return: ``{ok, git: {code, message}, api: {code, message},
            provider}`` or ``{ok: False, error}``
        :rtype: dict
        """
        self._sec()._check_can_manage_settings()
        provider = request.env['cloud.git.provider'].browse(
            int(provider_id),
        ).exists()
        if not provider:
            return {'ok': False, 'error': _("Git provider not found.")}
        ref = parse_provider_repo_url(url or '', [provider.base_url])
        if not ref:
            return {'ok': False, 'error': _(
                "This is not the URL of a repository of %s.", provider.name,
            )}
        if not consume_git_read(request.env):
            return {'ok': False, 'error': _(
                "Git read limit reached. Try again later."
            )}
        cred = provider._git_read_credential(ref.path)
        git_code = self._test_git_channel(provider, ref, cred)
        api_code = self._test_api_channel(cred)
        provider.sudo().write({
            'last_check_at': fields.Datetime.now(),
            'last_check_git': git_code,
            'last_check_api': api_code,
        })
        return {
            'ok': True,
            'git': {'code': git_code, 'message': git_error_message(git_code)},
            'api': {'code': api_code, 'message': git_error_message(api_code)},
            'provider': _serialize_provider(provider, True),
        }

    @staticmethod
    def _test_git_channel(provider, ref, cred):
        """Return the code of ``ls-remote HEAD`` on *ref* with *cred*.

        :param provider: the ``cloud.git.provider`` tested
        :param ref: the repository of it to read (``RepoRef``)
        :param cred: the ``cloud.git.credential`` git reads it with, or an
            empty recordset to read it as a public one
        :return: ``ok``, or the code of the failure
        :rtype: str
        """
        try:
            provider._git_reader().ls_remote(
                ref, cred._git_pair() if cred else None, ['HEAD'],
            )
        except GitReaderError as exc:
            return exc.code
        except ValueError:
            return 'not_found'
        return 'ok'

    @staticmethod
    def _test_api_channel(cred):
        """Return the code of the introspection of *cred*.

        :param cred: the ``cloud.git.credential`` the git channel read
            with, or an empty recordset
        :return: ``ok``; ``no_credential`` without one; ``no_api`` for a
            deploy token or one GitLab answers without API; or the code
            of the failure
        :rtype: str
        """
        if not cred:
            return 'no_credential'
        if cred.kind == 'deploy':
            return 'no_api'
        try:
            return 'ok' if cred._introspect() else 'no_api'
        except UserError as exc:
            return _cause_code(exc)
