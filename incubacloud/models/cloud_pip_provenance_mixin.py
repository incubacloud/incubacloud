"""Per-package authority over ``pip_dependencies``.

A repo line without a pinned commit floats to the tip of its branch on
every rebuild, so the code we ship keeps moving while the dependency
list stays where it was the day the line was created. Re-reading each
repo's ``requirements.txt`` at rebuild time closes that gap, but only if
we can tell *who wrote* each line: an upstream project bumping its own
dependency is maintenance to apply, while the same change on a line the
operator pinned by hand is a disagreement that needs a human.

This mixin stores that authority next to the field it describes. The map
holds only packages owned by a repo — no entry means the operator owns
the line — so an empty map (every record before this feature) behaves
exactly as before and needs no migration.

Shared by ``cloud.project`` and ``cloud.instance``: the same field with
the same write-time invariant, kept in one place per the P13 decision
(two byte-identical copies is how a fix lands on one and misses the
other).
"""
from odoo import fields, models

from ._repo_requirements import prune_pip_sources


class CloudPipProvenanceMixin(models.AbstractModel):
    _name = 'cloud.pip.provenance.mixin'
    _description = 'Pip dependency provenance (shared base)'

    pip_dependency_sources = fields.Json(
        string='Pip Dependency Sources',
        copy=False,
        help="Which repository authored each pip dependency line. "
             "Maintained automatically; packages absent from this map "
             "are treated as operator-owned and are never overwritten "
             "by an upstream change.",
    )

    def write(self, vals):
        """Drop provenance for lines this write took over.

        Any write that changes ``pip_dependencies`` outside the managed
        paths (the requirements merge and the conflict-resolution
        endpoint, which set ``pip_provenance_managed`` in the context) is
        a human editing the list, so entries whose recorded spec no
        longer appears verbatim stop being upstream's.
        """
        result = super().write(vals)
        if ('pip_dependencies' not in vals
                or self.env.context.get('pip_provenance_managed')):
            return result
        for record in self:
            current = record.pip_dependency_sources or {}
            pruned = prune_pip_sources(record.pip_dependencies or '', current)
            if pruned != current:
                # Managed flag on the recursive write: it carries no
                # ``pip_dependencies`` key, so the guard above already
                # stops the recursion — the flag documents the intent.
                record.with_context(
                    pip_provenance_managed=True,
                ).write({'pip_dependency_sources': pruned})
        return result
