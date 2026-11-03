"""Reload the Spanish texts of this module (the move of an instance between hosts).

Odoo updates a template's or a record's English text on upgrade but keeps
an existing translation, so without this the Spanish would keep the old
wording of what the move of an instance between hosts rewrote.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Overwrite the module's translations in every installed language."""
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    langs = [code for code, _name in env['res.lang'].get_installed()]
    env['ir.module.module']._load_module_terms(
        ['incubacloud'], langs, overwrite=True,
    )
