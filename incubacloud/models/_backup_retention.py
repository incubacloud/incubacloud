"""What a backup retention may be: duplicity's ``<count><unit>``.

The retention of a backup destination reaches the backup container as
``dup --force remove-older-than <retention> $DST``, which its job runner
starts through a shell. Only the form checked it (``validators.js``), so
a value written any other way went through as it was: ``1m`` is one
minute to duplicity, which deletes every chain but the active one, and
anything after a ``;`` is a command of its own (15-nov-2026). The panel
accepts days, weeks, months and years, a count of at least one, in
upper case: what duplicity and the panel's day count
(``incubacloud_pitr.retention_days``) read alike.
"""
import re

#: A retention the backup container may apply, matched whole
#: (``fullmatch``): ``[0-9]`` rather than ``\d``, which takes any
#: Unicode digit.
BACKUP_RETENTION_RE = re.compile(r"[1-9][0-9]*[DWMY]")

#: The retention the doodba template renders when nothing overrides it.
TEMPLATE_BACKUP_RETENTION = "3M"


def valid_backup_retention(value):
    """Whether *value* is a retention the backup container may apply.

    Surrounding spaces are ignored, as every reader strips them.

    :param str value: the retention as stored (``7D``, ``3M``, ``1Y``)
    :rtype: bool
    """
    return bool(BACKUP_RETENTION_RE.fullmatch((value or "").strip()))
