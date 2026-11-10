"""Personal defaults on the panel's models are an Administrator's.

``ir.default`` keeps, besides the defaults an administrator sets for
everyone, rows of a single user's, which any internal user may write for
themselves. The ORM applies them to every ``create`` that leaves the
field out, including those the panel runs with ``sudo`` once its own
checks passed, as ``sudo`` keeps the user. On the panel's models a value
nobody checked would then be written past those checks, so below
Administrator nobody keeps such rows, and those that exist are not read.
"""

import json

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError
from odoo.tools import SQL

#: The prefixes of the names of this module's models and of the modules
#: built on it.
PANEL_MODEL_PREFIXES = ("cloud.", "incubacloud.")


def is_panel_model(model_name):
    """Return whether *model_name* names one of the panel's models.

    :param str model_name: a model's technical name
    :rtype: bool
    """
    return bool(model_name) and model_name.startswith(PANEL_MODEL_PREFIXES)


class IrDefault(models.Model):
    """Personal defaults on the panel's models: an Administrator's only."""

    _inherit = "ir.default"

    @api.model
    def _keeps_personal_panel_defaults(self):
        """Return whether this user's own defaults on the panel's models count.

        The superuser's and an Administrator's (``base.group_system``)
        do; whatever the environment, as a ``sudo`` keeps the user.

        :rtype: bool
        """
        return self.env.uid == SUPERUSER_ID or self.env.user.has_group(
            "base.group_system",
        )

    @api.model
    def _check_may_set_personal_panel_default(self, model_name):
        """Refuse a personal default on *model_name* to whom may not keep one.

        :param str model_name: the model the default is for
        :raises AccessError: below Administrator, on one of the panel's
            models, unless the environment is the superuser's
        """
        if (
            self.env.su
            or not is_panel_model(model_name)
            or self._keeps_personal_panel_defaults()
        ):
            return
        raise AccessError(self.env._(
            "Only an Administrator sets personal default values on the "
            "records of IncubaCloud.",
        ))

    def _check_personal_panel_defaults(self):
        """Hold the personal rows of ``self`` to the rule of the panel's models.

        :raises AccessError: as
            :meth:`_check_may_set_personal_panel_default`
        """
        for default in self.sudo():
            if default.user_id:
                self._check_may_set_personal_panel_default(
                    default.field_id.model,
                )

    @api.model_create_multi
    def create(self, vals_list):
        """Create the defaults; personal ones on the panel's models as the rule says."""
        defaults = super().create(vals_list)
        defaults._check_personal_panel_defaults()
        return defaults

    def write(self, vals):
        """Write the defaults; personal ones on the panel's models as the rule says.

        ``set`` writes through this method and :meth:`create`.
        """
        result = super().write(vals)
        self._check_personal_panel_defaults()
        return result

    @api.model
    def _get_model_defaults(self, model_name, condition=False):
        """Return the defaults of *model_name*, this user's own if they count.

        On the panel's models, below Administrator, the rows of a single
        user's are left out (:meth:`_keeps_personal_panel_defaults`): the
        defaults are those of everyone, and of the company.
        """
        defaults = super()._get_model_defaults(model_name, condition=condition)
        if (
            not defaults
            or not is_panel_model(model_name)
            or self._keeps_personal_panel_defaults()
        ):
            return defaults
        self.flush_model()
        self.env.cr.execute(SQL(
            """
            SELECT f.name, d.json_value
              FROM ir_default d
              JOIN ir_model_fields f ON d.field_id = f.id
             WHERE f.model = %s
               AND d.user_id IS NULL
               AND (d.company_id IS NULL OR d.company_id = %s)
               AND d.condition IS NOT DISTINCT FROM %s
             ORDER BY d.company_id, d.id
            """,
            model_name, self.env.company.id or None, condition or None,
        ))
        shared = {}
        for name, json_value in self.env.cr.fetchall():
            # The company's first, as Odoo orders them.
            shared.setdefault(name, json.loads(json_value))
        return shared
