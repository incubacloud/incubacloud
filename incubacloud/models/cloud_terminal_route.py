"""Instance-scoped terminal routing table.

Thin concrete model over ``cloud.terminal.route.mixin`` — the whole
routing schema and liveness/GC/resolve logic is shared plumbing (see the
mixin). This model exists only to give the instance terminal its own
table, independent of the host shell's (saas) routing table.
"""
from odoo import models


class CloudTerminalRoute(models.Model):
    _name = 'cloud.terminal.route'
    _inherit = 'cloud.terminal.route.mixin'
    _description = 'Terminal subprocess routing table'

    _SESSION_MODEL = 'cloud.instance.session'
