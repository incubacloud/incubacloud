"""Pre-migrate for 1.0.200 — stop if two projects share a remote folder.

A project's instances live in ``~/<remote folder>/<instance>`` on a host
account's home, and nothing made the folder unique: a project given
another's folder would remove, overwrite or move onto that project's
directories. 1.0.200 adds a unique index over ``lower(remote_folder)``
(``cloud.project._remote_folder_uniq``). Odoo does not fail an upgrade
whose index cannot be built; it logs a warning and goes on, leaving the
database without the protection. So the duplicates are looked for first,
here, and the upgrade stops naming them.

No data is changed: which project keeps the folder, and where the other
one's instances really live on the hosts, is a decision for a person.
Production has no duplicates; this is for the databases we cannot see.
After renaming the folders (and the directories on the hosts to match),
run the upgrade again.
"""
from odoo.exceptions import UserError


def migrate(cr, version):
    """Raise when two projects have the same remote folder, case aside.

    :param cr: database cursor
    :param version: module version being upgraded from
    :raises UserError: naming every folder in use by more than one
        project, with the projects that hold it
    """
    if not version:
        return
    duplicates = _duplicated_folders(cr)
    if not duplicates:
        return
    lines = "; ".join(
        "'%s': %s" % (
            folder,
            ", ".join("%s (id %s)" % (name, pid) for pid, name in projects),
        )
        for folder, projects in duplicates
    )
    raise UserError(
        "Upgrade stopped: these cloud projects share a remote folder "
        "(compared without regard to case), and 1.0.200 makes it unique: "
        + lines
        + ". Give each its own remote folder, renaming the directories "
        "of its instances on the hosts to match, and run the upgrade "
        "again."
    )


def _duplicated_folders(cr):
    """Return the remote folders more than one project has.

    :param cr: database cursor
    :return: ``[(folder, [(project_id, name), ...]), ...]``, the folder
        lower-cased, ordered by folder and project id
    """
    cr.execute(
        """
        SELECT lower(remote_folder), id, COALESCE(name->>'en_US', name::text)
          FROM cloud_project
         WHERE remote_folder IS NOT NULL AND remote_folder <> ''
           AND lower(remote_folder) IN (
                SELECT lower(remote_folder)
                  FROM cloud_project
                 WHERE remote_folder IS NOT NULL AND remote_folder <> ''
              GROUP BY lower(remote_folder)
                HAVING count(*) > 1
           )
      ORDER BY 1, 2
        """
    )
    grouped = {}
    for folder, pid, name in cr.fetchall():
        grouped.setdefault(folder, []).append((pid, name))
    return list(grouped.items())
