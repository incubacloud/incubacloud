"""Single source of truth for the Odoo series Incubacloud can manage.

Used by the ``odoo_version`` Selection on ``cloud.project`` /
``cloud.instance`` and by the project importer when inferring a repo's
Odoo version. Keep this list as the only place the supported series are
declared so the selector and the importer can never drift apart.
"""

# Ordered oldest → newest. The newest entry is the usual default.
ODOO_VERSIONS = (
    '7.0', '8.0', '9.0', '10.0', '11.0', '12.0',
    '13.0', '14.0', '15.0', '16.0', '17.0', '18.0', '19.0',
)

# Ready-to-use value for ``fields.Selection(selection=...)``.
ODOO_VERSION_SELECTION = [(v, v) for v in ODOO_VERSIONS]
