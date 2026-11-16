"""Reload the Spanish texts of this module (managed backup storage).

Spanish says almacenamiento gestionado de copias for managed backup
storage, also in the name of the cron of unused managed backends. Odoo
keeps a record's existing translation on upgrade when its English text
is unchanged, so without this a running panel would keep the old name.
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
