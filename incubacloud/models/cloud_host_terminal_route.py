"""Routing table for subprocess-backed host-shell sessions.

Thin concrete model over ``cloud.terminal.route.mixin``. The routing
schema and liveness/GC/resolve logic is generic plumbing shared with the
instance terminal; only the *table* is separate, so each shell reconciles
its own audit model and a host-shell token never shares a row set with
the container terminal's.
"""
from odoo import models


class CloudHostTerminalRoute(models.Model):
    _name = 'cloud.host.terminal.route'
    _inherit = 'cloud.terminal.route.mixin'
    _description = 'Host console subprocess routing table'

    _SESSION_MODEL = 'cloud.host.session'
