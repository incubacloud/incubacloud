# 8.0 checks every call against the session's password, which a session
# opened by /ic/login gets from ``res_users`` (see ``session_password``).
# From 9.0 on a session has a token of its own, and nothing loads here.
# ``openerp`` first: on 9.0 and before, ``odoo`` names the source tree's
# ``odoo.py`` launcher. From 10.0 on there is no ``openerp``.
try:
    from openerp.release import version_info
except ImportError:
    from odoo.release import version_info

if version_info[0] == 8:
    from . import res_users
