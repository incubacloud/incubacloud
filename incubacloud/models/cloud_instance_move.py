from odoo import _, models


class CloudInstanceMove(models.Model):
    """Host-move concerns of ``cloud.instance``, kept out of the main file.

    New move-related behaviour lands here rather than growing
    ``cloud_instance.py``; the move chain itself still lives there until
    it is decomposed.
    """

    _inherit = "cloud.instance"

    _DNS_REPOINT_ALERT_CODE = "move_dns_repoint"

    def _move_dns_is_managed(self):
        """Whether something re-points DNS at the new host automatically.

        False on a bare core install: the A-record belongs to whoever
        runs the panel, so after a cutover the instance answers on a
        different IP and stays unreachable until a human updates it. The
        SaaS layer overrides this for tenants whose DNS it manages.
        """
        self.ensure_one()
        return False

    def _notify_move_dns_repoint(self, target_host):
        """Raise a warning alert asking the operator to re-point DNS.

        No-op when DNS is managed for this instance. The alert is keyed
        by ``code`` + ``instance_id`` so a second move updates the
        existing one instead of stacking duplicates.

        :param target_host: the ``cloud.host`` the instance moved to.
        :return: the ``cloud.alert`` raised, or ``None``.
        """
        self.ensure_one()
        if self._move_dns_is_managed():
            return None
        Alert = self.env["cloud.alert"].sudo()
        vals = {
            "code": self._DNS_REPOINT_ALERT_CODE,
            "level": "warning",
            "message": _(
                "Instance '%(instance)s' moved to host '%(host)s' — "
                "re-point %(domain)s to %(ip)s or it stays unreachable.",
                instance=self.name,
                host=target_host.name,
                domain=self.domain or _("its domain"),
                ip=target_host.ip_address or _("the new host IP"),
            ),
            "instance_id": self.id,
        }
        if self.project_id:
            vals["project_id"] = self.project_id.id
        if target_host:
            vals["host_id"] = target_host.id
        existing = Alert.search(
            [
                ("code", "=", self._DNS_REPOINT_ALERT_CODE),
                ("instance_id", "=", self.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )
        if existing:
            existing.write(vals)
            return existing
        return Alert.create(vals)
