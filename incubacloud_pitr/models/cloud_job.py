"""Where continuous recovery's job types sit among the panel's jobs."""
from odoo import api, models

#: Background jobs: run by the crons, kept out of the job drawer.
_HIDDEN = ["pitr_status", "pitr_base_backup"]


class CloudJob(models.Model):
    _inherit = "cloud.job"

    def _get_hidden_job_types(self):
        """The watch and the daily base copy are background jobs."""
        return super()._get_hidden_job_types() + _HIDDEN

    @api.model
    def _get_job_type_min_group(self):
        """Managers switch it, restore with it; the crons run the rest."""
        return super()._get_job_type_min_group() | {
            "pitr_enable": "group_cloud_manager",
            "pitr_disable": "group_cloud_manager",
            "pitr_restore": "group_cloud_manager",
            "pitr_status": "group_cloud_manager",
            "pitr_base_backup": "group_cloud_manager",
        }

    def _get_health_blocking_job_types(self):
        """Switching and restoring restart the database on purpose."""
        return super()._get_health_blocking_job_types() | {
            "pitr_enable", "pitr_disable", "pitr_restore",
        }
