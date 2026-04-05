"""Centralised permission checks for IncubaCloud.

Every security-relevant action goes through the ``_check_can_*`` helpers
defined here.  Both ORM overrides (``write``/``unlink``) and HTTP
controllers call the same methods, so the rules are enforced identically
regardless of access channel (SPA, XML-RPC, JSON-RPC, shell).
"""

from odoo import models
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
        raise AccessError(
            f"This action requires the '{min_group}' role or higher."
        )

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

    def _check_can_manage_hosts(self):
        self._check_cloud_group('group_cloud_manager')

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
            'can_manage_backups': is_at_least['developer'],
            'can_export': is_at_least['developer'],
            'can_clone_to_staging': is_at_least['developer'],
            'can_delete_production': is_at_least['manager'],
            'can_deploy': is_at_least['consultant'],
            'can_create_instance': is_at_least['consultant'],
            'can_edit_instance': is_at_least['consultant'],
            'can_edit_project': is_at_least['consultant'],
            'role': role,
        }
