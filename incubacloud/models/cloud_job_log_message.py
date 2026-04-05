from odoo import fields, models


class CloudJobLogMessage(models.Model):
    _name = "cloud.job.log.message"
    _description = "Job Log Message"
    _inherit = ["bus.listener.mixin"]
    _order = "create_date desc"

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
    )
    content = fields.Text(
        required=True,
    )

    def _format(self):
        return [{
            "id": msj.id,
            "content": msj.content,
            "create_date": msj.create_date,
        } for msj in self]
