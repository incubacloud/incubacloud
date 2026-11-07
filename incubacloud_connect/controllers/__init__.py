# /ic/login needs the session token of 9.0 and later: 7.0 has no
# ``openerp.http`` to route it, and 8.0 checks every call against the
# session's password, which a magic link never has. There the module
# installs without its route.
# ``openerp`` first: on 9.0 and before, ``odoo`` names the source tree's
# ``odoo.py`` launcher. From 10.0 on there is no ``openerp``.
try:
    from openerp.release import version_info
except ImportError:
    from odoo.release import version_info

if version_info[0] >= 9:
    from . import connect
