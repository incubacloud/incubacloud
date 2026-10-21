"""Single source of truth for the Odoo series Incubacloud can manage.

Every series has a card here saying what distinguishes it from the
others, and nothing else in the panel decides on its own: the version
pickers, the project importer, the PostgreSQL picker, the executors and
the scripts all read from this table. A series that needs something new
gets a field added to the card, not another ``if version`` somewhere.

Each card is written out in full on purpose. A card that inherited from
the series before it would carry a mistake forward in silence;
``test_odoo_versions`` prints the resolved table so a change is read
whole.

Fields of a card:

``offered``
    Whether the series appears in the pickers and the importer accepts
    it. A series being prepared lives here with ``offered=False`` until
    its QA passes, so the code that supports it can land without a
    customer deploying it.
``postgres``
    ``(lowest, highest, default)`` major versions of PostgreSQL the
    series runs on. The picker shows that range and ``cloud.instance``
    refuses any other.
``db_tool``
    The tool that dumps and loads the instance's database inside its
    ``odoo`` container, one of ``DB_TOOLS``. ``scripts/lib/common.sh``
    implements each (``ic_db_dump_args`` / ``ic_db_load_args``) and the
    executors name the one in use through ``IC_DB_TOOL``.
``template_min``
    The lowest doodba copier template (``vX.Y.Z``) able to render the
    series, or None when any does. A deploy or rebuild whose pin is
    older refuses before copier fails on its own.
"""
import re

#: The database tools ``scripts/lib/common.sh`` implements.
#: ``click-odoo``: ``click-odoo-backupdb`` / ``click-odoo-restoredb``
#: (click-odoo-contrib, every series up to 19). ``odoo-db``: ``odoo db
#: dump`` / ``odoo db load`` (Odoo 18 and later; the only tool on 20,
#: where click-odoo-contrib 1.24 refuses to run its database commands).
DB_TOOLS = ("click-odoo", "odoo-db")

_TEMPLATE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _card(offered, postgres, db_tool, template_min):
    """Build one series' card, checking its values.

    :param bool offered: see the module docstring.
    :param tuple postgres: ``(lowest, highest, default)`` majors as ints.
    :param str db_tool: one of ``DB_TOOLS``.
    :param template_min: ``vX.Y.Z`` or None.
    :rtype: dict
    """
    lowest, highest, default = postgres
    if not lowest <= default <= highest:
        raise ValueError(f"PostgreSQL default {default} outside {lowest}-{highest}")
    if db_tool not in DB_TOOLS:
        raise ValueError(f"unknown db tool {db_tool!r}")
    if template_min is not None and not _TEMPLATE_TAG_RE.match(template_min):
        raise ValueError(f"template_min must be a vX.Y.Z tag, got {template_min!r}")
    return {
        "offered": bool(offered),
        "postgres": (int(lowest), int(highest), int(default)),
        "db_tool": db_tool,
        "template_min": template_min,
    }


# Ordered oldest → newest. The newest offered entry is the usual default.
VERSIONS = {
    "7.0": _card(True, (14, 18, 17), "click-odoo", None),
    "8.0": _card(True, (14, 18, 17), "click-odoo", None),
    "9.0": _card(True, (14, 18, 17), "click-odoo", None),
    "10.0": _card(True, (14, 18, 17), "click-odoo", None),
    "11.0": _card(True, (14, 18, 17), "click-odoo", None),
    "12.0": _card(True, (14, 18, 17), "click-odoo", None),
    "13.0": _card(True, (14, 18, 17), "click-odoo", None),
    "14.0": _card(True, (14, 18, 17), "click-odoo", None),
    "15.0": _card(True, (14, 18, 17), "click-odoo", None),
    "16.0": _card(True, (14, 18, 17), "click-odoo", None),
    "17.0": _card(True, (14, 18, 17), "click-odoo", None),
    "18.0": _card(True, (14, 18, 17), "click-odoo", None),
    "19.0": _card(True, (14, 18, 17), "click-odoo", None),
}


def card(version):
    """Return the card of *version*.

    :param str version: a series, ``"19.0"``.
    :raises KeyError: for a series the panel does not know.
    :rtype: dict
    """
    return VERSIONS[str(version)]


def offered_versions():
    """Return the series the pickers offer, oldest first.

    :rtype: tuple
    """
    return tuple(v for v, c in VERSIONS.items() if c["offered"])


def postgres_versions(version):
    """Return the PostgreSQL majors *version* runs on, as the picker's strings.

    :param str version: a series.
    :rtype: tuple
    """
    lowest, highest, _default = card(version)["postgres"]
    return tuple(str(major) for major in range(lowest, highest + 1))


def postgres_default(version):
    """Return the PostgreSQL major a new instance of *version* gets.

    :param str version: a series.
    :rtype: str
    """
    return str(card(version)["postgres"][2])


def db_tool(version):
    """Return the database tool of *version*, one of ``DB_TOOLS``.

    :param str version: a series.
    :rtype: str
    """
    return card(version)["db_tool"]


def script_env(version):
    """Return the environment the operation scripts run with for *version*.

    What a script needs to know of the series and no operation passes
    by argument: today the database tool (``IC_DB_TOOL``,
    ``scripts/lib/common.sh``). Handed to
    ``AbstractExecutor.run_script`` as ``env``; it reads the version
    alone, so an executor's test may describe its instance as plain data.

    :param str version: a series.
    :rtype: dict
    """
    return {"IC_DB_TOOL": db_tool(version)}


def template_ref_allows(version, ref):
    """Whether doodba template *ref* can render *version*.

    :param str version: a series.
    :param ref: the pin, a ``vX.Y.Z`` tag; empty or a branch name means
        the template's default branch, which is newer than any tag and
        is therefore allowed.
    :rtype: bool
    """
    needed = card(version)["template_min"]
    if needed is None:
        return True
    pinned = _TEMPLATE_TAG_RE.match((ref or "").strip())
    if not pinned:
        return True
    floor = _TEMPLATE_TAG_RE.match(needed)
    return tuple(map(int, pinned.groups())) >= tuple(map(int, floor.groups()))


def picker_facts():
    """Return what the pickers need of each offered series, JSON-shaped.

    :return: ``{version: {"postgres": [...], "postgres_default": "17"}}``
    :rtype: dict
    """
    return {
        version: {
            "postgres": list(postgres_versions(version)),
            "postgres_default": postgres_default(version),
        }
        for version in offered_versions()
    }


#: The offered series, kept under the historical name for its readers:
#: what the pickers show and the importer accepts.
ODOO_VERSIONS = offered_versions()

#: Ready-to-use value for ``fields.Selection(selection=...)``: every
#: series with a card, offered or not. An instance keeps a valid value
#: when its series stops being offered, and a series being prepared can
#: be deployed by QA before customers see it in a picker.
ODOO_VERSION_SELECTION = [(v, v) for v in VERSIONS]
