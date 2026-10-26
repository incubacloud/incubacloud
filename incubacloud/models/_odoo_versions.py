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
    Whether the series appears in the pickers, the importer accepts it
    and anyone may choose it (``may_choose``). A series being prepared
    lives here with ``offered=False`` until its QA passes, so the code
    that supports it can land without a customer deploying it. A panel
    offers it only if its pinned template can render it
    (``template_min``), and the newest one it offers is the series a
    new project, or an instance outside one, gets (``default_version``).
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


# Ordered oldest → newest. The newest offered entry a panel's pin can
# render is the series a new project, or an instance outside one, gets
# (``default_version``); the platform's own panels name theirs.
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
    # Offered since core 1.0.198 (plan Odoo 20, phase 2); the run of the
    # series (``qa-run -m versiones``) tests it with the rest. PostgreSQL 16
    # to 18, as the template requires; odoo db, since click-odoo-contrib
    # 1.24 refuses its database commands on 20 (lab of 22-oct-2026);
    # template v10.0.3, the first whose tasks also use odoo db on 20
    # (v10.0.2 renders it, but its ``invoke resetdb`` would fail in an
    # exported copy).
    "20.0": _card(True, (16, 18, 17), "odoo-db", "v10.0.3"),
}


def card(version):
    """Return the card of *version*.

    :param str version: a series, ``"19.0"``.
    :raises KeyError: for a series the panel does not know.
    :rtype: dict
    """
    return VERSIONS[str(version)]


def offered_versions(template_ref=None):
    """Return the series the pickers offer, oldest first.

    :param template_ref: the panel's pinned doodba template
        (``pinned_template_ref``). A series it cannot render
        (``template_ref_allows``) is left out: that panel could not
        deploy it. None, empty or a branch name leave none out.
    :rtype: tuple
    """
    return tuple(
        v for v, c in VERSIONS.items()
        if c["offered"] and template_ref_allows(v, template_ref)
    )


def default_version(template_ref=None):
    """Return the series a new project, or an instance outside one, gets.

    Always the newest offered one (owner's decision of 22-oct-2026),
    among those *template_ref* can render, so a panel pinned below a
    series' floor does not start projects on a series it cannot deploy.

    :param template_ref: as for ``offered_versions``.
    :rtype: str
    """
    return offered_versions(template_ref)[-1]


def may_choose(version, user, template_ref=None):
    """Whether *user* may give a project or an instance the series *version*.

    An offered series the panel's template can render, anyone. One that
    is not — being prepared, retired, or too new for the pin — only an
    Odoo administrator (``base.group_system``): QA deploys a series
    before customers see it, and on a customer's own panel that group is
    ours alone; an administrator's deploy below the floor is still
    refused, naming RB-15. A record that already holds a series keeps
    it; this is asked when the series is chosen, and a save that sends
    back the series a record holds chooses nothing
    (``without_unchanged_series``).

    :param str version: a series.
    :param user: the ``res.users`` record choosing it.
    :param template_ref: as for ``offered_versions``.
    :rtype: bool
    """
    return (
        card(version)["offered"] and template_ref_allows(version, template_ref)
    ) or user.has_group("base.group_system")


def without_unchanged_series(records, vals):
    """Return the values of a write, without a series the records already hold.

    The panel's forms send ``odoo_version`` back with every save. Written
    again, it would be asked of ``may_choose`` as if chosen now, and on a
    panel whose pin went back below a series' floor (RB-15) nobody but an
    administrator could save a project or an instance of that series.
    Called by the ``write`` of ``cloud.project`` and ``cloud.instance``.

    :param records: the records being written.
    :param dict vals: the values of the write; not modified.
    :return: *vals* itself, or a copy without ``odoo_version`` when every
        record already holds that value.
    :rtype: dict
    """
    if "odoo_version" in vals and all(
        record.odoo_version == vals["odoo_version"] for record in records
    ):
        return {k: v for k, v in vals.items() if k != "odoo_version"}
    return vals


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


def picker_facts(template_ref=None):
    """Return what the pickers need of each offered series, JSON-shaped.

    :param template_ref: as for ``offered_versions``.
    :return: ``{version: {"postgres": [...], "postgres_default": "17"}}``
    :rtype: dict
    """
    return {
        version: {
            "postgres": list(postgres_versions(version)),
            "postgres_default": postgres_default(version),
        }
        for version in offered_versions(template_ref)
    }


def pinned_template_ref(env):
    """Return the doodba template this panel deploys with.

    The Settings pin (``cloud.settings.copier_template_ref``), read as
    the system: it decides what every user of the panel is offered,
    whether or not they may read the settings.

    :param env: an Odoo environment.
    :return: the pin; empty for the template's default branch.
    :rtype: str
    """
    return env["cloud.settings"]._get_system().copier_template_ref or ""


def new_record_default(model):
    """Default ``odoo_version`` of a new ``cloud.project`` or ``cloud.instance``.

    ``default_version`` under this panel's pin. A default only fills a
    record being created: the rows that exist keep their series. An
    instance created in a project takes the project's series instead
    (``cloud.instance.create``).

    :param model: the model of the new record, which Odoo passes to a
        field's default.
    :rtype: str
    """
    return default_version(pinned_template_ref(model.env))


#: The offered series, kept under the historical name for its readers:
#: what the importer accepts, whatever a panel's pin.
ODOO_VERSIONS = offered_versions()

#: Ready-to-use value for ``fields.Selection(selection=...)``: every
#: series with a card, offered or not. An instance keeps a valid value
#: when its series stops being offered, and a series being prepared can
#: be deployed by QA before customers see it in a picker.
ODOO_VERSION_SELECTION = [(v, v) for v in VERSIONS]
