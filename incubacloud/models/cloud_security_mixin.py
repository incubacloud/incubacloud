"""Centralised permission checks for IncubaCloud.

Every security-relevant action goes through the ``_check_can_*`` helpers
defined here.  Both ORM overrides (``write``/``unlink``) and HTTP
controllers call the same methods, so the rules are enforced identically
regardless of access channel (SPA, XML-RPC, JSON-RPC, shell).
"""

from odoo import _, fields, models
from odoo.exceptions import AccessError
from odoo.tools import clean_context

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


def defaults_as_sent(model, vals_list):
    """Write in *vals_list* the defaults its caller chose, as if sent.

    The ORM fills the fields a ``create`` was not sent with their
    defaults after the ``create`` checked its values: those of the
    context (``default_*``, which an RPC call brings with it) and the
    caller's own rows of ``ir.default`` come from the caller, and a
    ``create`` that runs with ``sudo`` applies them past the access rights
    and record rules too. For a caller without superuser rights they are
    written in the values here, before any check, and the model returned
    has a context without ``default_*`` keys: the ORM does not apply them
    again, nor to the rows the ``create`` makes itself. A field's own
    default stays the ORM's, and so do the rows of ``ir.default`` an
    administrator sets for everyone or for a company.

    :param model: the model ``create`` was called on
    :param list vals_list: the values of each record created, completed
        in place
    :return: *model*, without the ``default_*`` keys in its context; the
        superuser's is returned as it is
    """
    if model.env.su:
        return model
    chosen = {
        key[len("default_"):]
        for key in model.env.context
        if key.startswith("default_")
    }
    chosen.update(_own_default_names(model))
    chosen.intersection_update(model._fields)
    Command = fields.Command
    for vals in vals_list:
        missing = [name for name in chosen if name not in vals]
        for name, value in model.default_get(missing).items():
            if model._fields[name].type in ("one2many", "many2many"):
                # ``default_get`` sends an x2many as a ``Command.SET`` of
                # the records it names, followed by the rows it creates,
                # or as ``Command.CLEAR``: on a new record, an empty set
                # or a clear does nothing.
                value = [
                    command for command in value
                    if command[0] not in (Command.SET, Command.CLEAR)
                    or command[2]
                ]
            vals[name] = value
    return model.with_context(clean_context(model.env.context))


def _own_default_names(model):
    """Return the fields of *model* its user has a default of their own for.

    The rows of ``ir.default`` of this user's alone, as
    ``_get_model_defaults`` applies them without a condition.

    :param model: a model, in the user's environment
    :rtype: set
    """
    env = model.env
    own = env["ir.default"].sudo().search_fetch(
        [
            ("field_id.model", "=", model._name),
            ("user_id", "=", env.uid),
            ("company_id", "in", (False, env.company.id)),
            ("condition", "=", False),
        ],
        ["field_id"],
    )
    return set(own.field_id.mapped("name"))


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
        rows (``Command.CREATE``) of a model the user may create, and so
        may the x2many fields of those rows; a many2many may also link
        records the user may read (``Command.LINK``, ``Command.SET``), as
        the forms do with tags.
        Rows created lose the fields *panel_only* names, which only the
        panel writes. Nothing is checked for the superuser.

        :param list vals_list: the values of each record created; the
            commands of *panel_only*'s fields are replaced in place
        :param dict panel_only: an x2many field's name -> the names of
            the fields taken out of each row it creates
        :return: the names of the fields that create rows, for
            :meth:`_check_new_record_rows`
        :rtype: set
        :raises AccessError: for any other command, a row of a model the
            user may not create, or a linked record the user may not read
        """
        if self.env.su:
            return set()
        panel_only = panel_only or {}
        creating = set()
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
                if any(
                    command[0] == fields.Command.CREATE
                    for command in vals[name]
                ):
                    creating.add(name)
        return creating

    def _check_new_record_rows(self, records, names):
        """Check the rows *records* created with the system's rights.

        After a ``create`` held by :meth:`_check_new_record_x2many`: the
        rows its fields *names* hold are checked in the user's
        environment, as Odoo checks a creation, with the access rights
        and the record rules of their model. Nothing is checked for the
        superuser.

        :param records: the records just created
        :param names: the x2many fields that created rows
        :raises AccessError: when the user may not create one of them
        """
        if self.env.su:
            return
        for name in sorted(names):
            records.sudo().mapped(name).with_env(self.env).check_access(
                'create',
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
                comodel = self.env[field.comodel_name]
                comodel.check_access('create')
                row = {
                    key: val
                    for key, val in (command[2] or {}).items()
                    if key not in dropped
                }
                for key, val in row.items():
                    subfield = comodel._fields.get(key)
                    if val and subfield is not None and subfield.type in (
                        'one2many', 'many2many',
                    ):
                        row[key] = self._x2many_create_commands(
                            subfield, val, (),
                        )
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
                'A new record can only create new rows in "%(field)s": '
                'existing ones cannot be added to it, changed or removed.',
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
