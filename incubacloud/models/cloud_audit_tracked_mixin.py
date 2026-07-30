"""Shared audit trail for config changes on tracked fields.

``cloud.project``, ``cloud.host`` and ``cloud.instance`` each kept a
private copy of the same snapshot/diff/log block inside ``write()``, and
the copies had already diverged once: the multi-record ``display_name``
fix (an x2many with two records makes ``display_name`` raise) landed on
project only. One implementation here; each model only says which fields
it tracks and how an audit row points back at it.
"""
from odoo import models


class CloudAuditTrackedMixin(models.AbstractModel):
    _name = 'cloud.audit.tracked.mixin'
    _description = 'Audit trail for tracked config fields'

    #: Overridden per model: the fields whose changes deserve an audit row.
    _AUDIT_TRACKED_FIELDS = frozenset()

    @staticmethod
    def _audit_display(value):
        """Render a tracked field value for the audit-log detail line.

        ``display_name`` raises on a multi-record set, which an x2many
        field such as ``member_ids`` is as soon as it holds more than
        one record — join their names instead.
        """
        if isinstance(value, models.BaseModel):
            return ', '.join(value.mapped('display_name')) if value else '∅'
        return str(value)

    def _audit_snapshot(self, vals):
        """Capture tracked fields' values before a ``write()``.

        :param vals: the values about to be written.
        :return: ``(changed, old_snap)`` — tracked field names present in
            ``vals``, and their per-record values before the write.
        """
        changed = self._AUDIT_TRACKED_FIELDS & set(vals)
        old_snap = {}
        if changed:
            old_snap = {f: {r.id: r[f] for r in self} for f in changed}
        return changed, old_snap

    def _audit_target_vals(self):
        """Values pointing a 'Config changed' audit row at this record.

        Each concrete model overrides this with its own foreign keys
        (``project_id`` / ``host_id`` / ``instance_id``).
        """
        self.ensure_one()
        return {}

    def _audit_log_changes(self, changed, old_snap):
        """Write one 'Config changed' audit row per record that changed.

        :param changed: tracked field names, from :meth:`_audit_snapshot`.
        :param old_snap: pre-write values, from :meth:`_audit_snapshot`.
        """
        if not changed:
            return
        for rec in self:
            parts = []
            for field in changed:
                old = old_snap[field][rec.id]
                new = rec[field]
                if old == new:
                    continue
                field_def = rec._fields[field]
                parts.append(
                    f"{field_def.string}: "
                    f"{self._audit_display(old)}→"
                    f"{self._audit_display(new)}"
                )
            if parts:
                self.env['cloud.audit.log'].sudo().create({
                    'action': 'Config changed',
                    'details': '; '.join(parts)[:255],
                    **rec._audit_target_vals(),
                })
