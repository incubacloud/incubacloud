# /ic/login needs the session API of 8.0 and later: 7.0 has no
# ``openerp.http`` to route it. 8.0 checks every call against the
# session's password, which ``models/res_users`` gives the session.
# ``openerp`` first: on 9.0 and before, ``odoo`` names the source tree's
# ``odoo.py`` launcher. From 10.0 on there is no ``openerp``.
try:
    from openerp.release import version_info
except ImportError:
    from odoo.release import version_info

if version_info[0] >= 8:
    from . import connect
