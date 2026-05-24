from odoo import models, fields


class CloudJobType(models.Model):
    _name = "cloud.job.type"
    _description = "Job type for remote execution"

    _sql_constraints = [
        ('code_uniq', 'unique (code)',
         'Job type code must be unique.'),
    ]

    name = fields.Char(
        string="Name"
    )
    code = fields.Char(
        string="Code",
        required=True,
    )
    apply_to = fields.Selection([
        ("host", "Cloud Host"),
        ("instance", "Cloud Instance"),
    ],
        string="Apply To",
        required=True,
    )
    description = fields.Text(
        string="Description"
    )
    show_as_action = fields.Boolean(
        string="Show as Action Button",
        default=False,
        help=(
            "If enabled, a button for this job type appears in the"
            " instance actions bar."
        ),
    )
    action_icon = fields.Char(
        string="Button Icon",
        default="fa-cog",
        help="FontAwesome class for the button (e.g. 'fa-magic', 'fa-cog').",
    )
    action_order = fields.Integer(
        string="Display Order",
        default=100,
        help="Lower values appear first in the custom actions bar.",
    )
    priority_tier = fields.Selection(
        [('high', 'High'), ('normal', 'Normal'), ('low', 'Low')],
        string="Priority Tier",
        default='normal',
        required=True,
        help=(
            "Determines the queue_job channel and priority."
            " Background/automated jobs (health checks, metrics, docker"
            " prune) should be 'low' so they never block user-initiated"
            " work. Jobs on a production instance are auto-promoted from"
            " 'normal' to 'high' at enqueue time."
        ),
    )
