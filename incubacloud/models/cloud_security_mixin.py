"""Centralised permission checks for IncubaCloud.

Every security-relevant action goes through the ``_check_can_*`` helpers
defined here.  Both ORM overrides (``write``/``unlink``) and HTTP
controllers call the same methods, so the rules are enforced identically
regardless of access channel (SPA, XML-RPC, JSON-RPC, shell).
"""

from odoo import _, fields, models
from odoo.exceptions import AccessError

_GROUP_HIERARCHY = (
    'group_cloud_user',
    'group_cloud_consultant',
    'group_cloud_project_manager',
    'group_cloud_developer',
    'group_cloud_manager',
)

# Maps XML-id suffixes to user-facing role names.
_ROLE_NAMES = {
    'user': 'stakeholder',
}


class CloudSecurityMixin(models.AbstractModel):
    _name = 'cloud.security.mixin'
    _description = 'Cloud Security Mixin'

    # ── Core check ──────────────────────────────────────────────────────

    def _check_cloud_group(self, min_group):
        """Raise ``AccessError`` unless the current user has *min_group*
        or any higher group in the hierarchy.
        """
        if self.env.su:
            return True
        idx = _GROUP_HIERARCHY.index(min_group)
        for grp in _GROUP_HIERARCHY[idx:]:
            if self.env.user.has_group(f'incubacloud.{grp}'):
                return True
        raise AccessError(_(
            "This action requires the '%s' role or higher.",
        ) % min_group)

    def _has_cloud_group(self, min_group):
        """Return ``True`` if the user meets the minimum group — no raise."""
        if self.env.su:
            return True
        idx = _GROUP_HIERARCHY.index(min_group)
        return any(
            self.env.user.has_group(f'incubacloud.{grp}')
            for grp in _GROUP_HIERARCHY[idx:]
        )

    # ── Semantic checks (used by models AND controllers) ────────────────

    def _check_can_create_project(self):
        self._check_cloud_group('group_cloud_project_manager')

    def _check_can_delete_project(self):
        self._check_cloud_group('group_cloud_manager')

    def _check_can_create_instance(self):
        self._check_cloud_group('group_cloud_consultant')

    def _check_can_delete_instance(self, instance):
        if instance.environment == 'production':
            self._check_cloud_group('group_cloud_manager')
        else:
            self._check_cloud_group('group_cloud_consultant')

    def _check_can_deploy(self):
        self._check_cloud_group('group_cloud_consultant')

    def _can_change_repo_url(self):
        """Whether the user may point a repository line at another repository.

        From Project Manager up (10-nov-2026): a Consultant changes a
        line's branch but not the repository it names.

        :rtype: bool
        """
        return self._has_cloud_group('group_cloud_project_manager')

    def _check_can_manage_hosts(self):
        self._check_cloud_group('group_cloud_manager')

    def _check_new_record_x2many(self, vals_list, panel_only=None):
        """Keep a record ``create`` writes as the system to rows of its own.

        For a model whose ``create`` runs with ``sudo`` on the user's
        behalf (``cloud.instance``, ``cloud.host``): there the access
        rights and record rules of the rows its x2many fields name are
        not checked. Every x2many value of *vals_list* may only create
        rows (``Command.CREATE``) of a model the user may create; a
        many2many may also link records the user may read
        (``Command.LINK``, ``Command.SET``), as the forms do with tags.
        Rows created lose the fields *panel_only* names, which only the
        panel writes. Nothing is checked for the superuser.

        :param list vals_list: the values of each record created; the
            commands of *panel_only*'s fields are replaced in place
        :param dict panel_only: an x2many field's name -> the names of
            the fields taken out of each row it creates
        :raises AccessError: for any other command, a row of a model the
            user may not create, or a linked record the user may not read
        """
        if self.env.su:
            return
        panel_only = panel_only or {}
        for vals in vals_list:
            for name, value in list(vals.items()):
                field = self._fields.get(name)
                if not value or field is None or field.type not in (
                    'one2many', 'many2many',
                ):
                    continue
                vals[name] = self._x2many_create_commands(
                    field, value, panel_only.get(name, ()),
                )

    def _x2many_create_commands(self, field, value, dropped):
        """Return *value* of *field* as commands a new record may take.

        See :meth:`_check_new_record_x2many`. A value that is not a list
        of commands (ids, a tuple, a recordset) means ``Command.SET``.

        :param field: the x2many field of this model
        :param value: what ``create`` received for it
        :param dropped: names of the fields taken out of each row created
        :return: the commands, the rows created without *dropped*
        :rtype: list
        :raises AccessError: as :meth:`_check_new_record_x2many`
        """
        Command = fields.Command
        if isinstance(value, models.BaseModel):
            value = [Command.set(value.ids)]
        elif not isinstance(value, list) or not isinstance(
            value[0], (list, tuple),
        ):
            value = [Command.set(tuple(value))]
        commands = []
        for command in value:
            code = command[0] if isinstance(command, (list, tuple)) else None
            if code == Command.CREATE:
                self.env[field.comodel_name].check_access('create')
                row = {
                    key: val
                    for key, val in (command[2] or {}).items()
                    if key not in dropped
                }
                commands.append((Command.CREATE, command[1], row))
                continue
            if field.type == 'many2many' and code in (
                Command.LINK, Command.SET,
            ):
                ids = [command[1]] if code == Command.LINK else command[2]
                self.env[field.comodel_name].browse(ids).check_access('read')
                commands.append(command)
                continue
            raise AccessError(_(
                "A new record can only create its %(field)s: those that "
                "already exist cannot be added to it, changed or removed.",
                field=field._description_string(self.env),
            ))
        return commands

    def _check_can_view_metrics(self):
        """Guard the metrics views and the proxy access log."""
        self._check_cloud_group('group_cloud_developer')

    def _check_can_manage_settings(self):
        self._check_cloud_group('group_cloud_manager')

    def _check_can_use_terminal(self):
        self._check_cloud_group('group_cloud_developer')

    def _check_can_view_logs(self):
        self._check_cloud_group('group_cloud_developer')

    def _check_can_manage_backups(self):
        self._check_cloud_group('group_cloud_developer')

    def _check_can_export(self):
        self._check_cloud_group('group_cloud_developer')

    def _check_can_clone_to_staging(self):
        self._check_cloud_group('group_cloud_developer')

    def _check_can_connect_as_user(self, instance=None, target_is_admin=False):
        """Gate connect-as by environment and by the target user's power.

        Staging (and runbot) keeps the historical floor: Stakeholder+,
        so a client watching their own staging can still impersonate.
        Production is stricter, mirroring Odoo.sh: a normal target user
        requires Developer, and the instance's own admin requires Manager.

        :param instance: the ``cloud.instance`` being connected to.
            ``None`` keeps the legacy Stakeholder+ behaviour so callers
            that do not resolve an instance are unaffected.
        :param target_is_admin: True when the impersonated user is an
            administrator of the instance's database.
        :raises AccessError: when the caller lacks the required role.
        """
        # Per-instance scoping is enforced separately by record rules on
        # cloud.instance — controllers must browse without sudo so the
        # rule applies.
        if instance is not None and instance.environment == 'production':
            if target_is_admin:
                self._check_cloud_group('group_cloud_manager')
            else:
                self._check_cloud_group('group_cloud_developer')
            return
        self._check_cloud_group('group_cloud_user')

    # ── Role introspection (for /cloud/get_config) ──────────────────────

    def _get_user_role(self):
        """Return the highest role name for the current user."""
        for grp in reversed(_GROUP_HIERARCHY):
            if self.env.user.has_group(f'incubacloud.{grp}'):
                short = grp.replace('group_cloud_', '')
                return _ROLE_NAMES.get(short, short)
        return 'none'

    def _get_user_permissions(self):
        """Return a dict of boolean permission flags for the frontend."""
        role = self._get_user_role()
        is_at_least = {
            g.replace('group_cloud_', ''): self._has_cloud_group(g)
            for g in _GROUP_HIERARCHY
        }
        return {
            'can_create_project': is_at_least['project_manager'],
            'can_manage_hosts': is_at_least['manager'],
            'can_manage_settings': is_at_least['manager'],
            'can_use_terminal': is_at_least['developer'],
            'can_view_logs': is_at_least['developer'],
            # Metrics and the proxy access log. From developer upwards
            # rather than manager-only: whoever debugs a slow or attacked
            # instance is the one who needs these, and requiring the
            # manager role turned every incident into a relay race.
            'can_view_metrics': is_at_least['developer'],
            'can_manage_backups': is_at_least['developer'],
            'can_export': is_at_least['developer'],
            'can_clone_to_staging': is_at_least['developer'],
            'can_delete_production': is_at_least['manager'],
            # ``_check_can_delete_project``: the project page shows its
            # delete buttons by it.
            'can_delete_project': is_at_least['manager'],
            'can_connect_as': is_at_least['user'],
            'can_connect_production': is_at_least['developer'],
            'can_connect_production_admin': is_at_least['manager'],
            'can_deploy': is_at_least['consultant'],
            'can_create_instance': is_at_least['consultant'],
            'can_edit_instance': is_at_least['consultant'],
            'can_edit_project': is_at_least['consultant'],
            # ``_can_change_repo_url``: a saved repository line's URL is
            # read-only below it.
            'can_change_repo_url': is_at_least['project_manager'],
            'role': role,
        }
