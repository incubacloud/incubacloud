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
    Whether the series appears in the pickers and anyone may choose it
    (``may_choose``); an import of code for one that is not offered is
    refused to anyone but an Odoo administrator
    (``import_series_refusal``). A series being prepared
    lives here with ``offered=False`` until its QA passes, so the code
    that supports it can land without a customer deploying it. A panel
    offers it only if its pinned template can render it
    (``template_min``), and the newest one it offers is the series a
    new project, or an instance outside one, gets (``default_version``).
``postgres``
    ``(lowest, highest, default)`` major versions of PostgreSQL the
    series runs on, as the strings of ``POSTGRES_MAJORS``. The picker
    shows that range and ``cloud.instance`` refuses any other. What Odoo
    and doodba's template recommend for the series, unless a reason
    written beside the card, with its date, says otherwise; then the
    nearest major that serves (owner, 7-nov-2026).
``postgres_beyond_template``
    Whether that range is not the one doodba's template allows the
    series (its copier.yml's ``pg_per_odoo``), so the copier run of a
    deploy and of a rebuild takes the card's range in its place
    (``copier_defaults``): copier refuses a PostgreSQL outside the
    template's.
``db_tool``
    The tool that dumps and loads the instance's database inside its
    ``odoo`` container, one of ``DB_TOOLS``. ``scripts/lib/common.sh``
    implements each (``ic_db_dump_args`` / ``ic_db_load_args``) and the
    executors name the one in use through ``IC_DB_TOOL``.
``neutralize``
    How a copy of the series is neutralized, one of ``NEUTRALIZERS``.
    ``scripts/lib/common.sh`` implements each and the scripts learn the
    one in use through ``IC_NEUTRALIZE``.
``template_min``
    The lowest doodba copier template (``vX.Y.Z``) able to render the
    series, or None when any does. A deploy or rebuild whose pin is
    older refuses before copier fails on its own.
``debian_archive``
    Whether doodba's image of the series is built on a Debian release
    gone from the mirrors, so its build reads apt from Freexian's
    Extended LTS of that release (``scripts/doodba/debian_archive.sh``,
    put in the image's build by ``deploy.sh debian-archive``). Without
    it the image of 8.0 to 10.0, on Debian 8, does not build, nor
    installs the packages of an ``apt.txt`` (QA of the series,
    7-nov-2026).
``headless``
    The option, one of ``HEADLESS_FLAGS``, that runs the series' Odoo
    with no server listening, so a one-off ``odoo -i`` never takes the
    port of an Odoo sharing its network: ``--no-http`` from 11.0, where
    Odoo's ``tools/config.py`` adds it, ``--no-xmlrpc`` before. Handed to
    ``restore.sh ensure-connect`` by its callers, not read from
    ``odoo --help``: from 19.0 the help lists the commands, not the
    server's options (review of the series' fixes, 7-nov-2026).
``prefetch_repos``
    Whether every repository of ``repos.yaml`` is fetched on the host,
    in a helper container with a current git, before the image is
    built with ``AGGREGATE=false``, instead of being cloned by the
    image's own git. 8.0 to 10.0: their git 2.1.4 (Debian 8) follows a
    302 to plain http on the same host and sends the token in clear,
    and knows no ``http.followRedirects`` (checked on 8-nov-2026).
"""
import re

#: The database tools ``scripts/lib/common.sh`` implements.
#: ``click-odoo``: ``click-odoo-backupdb`` / ``click-odoo-restoredb``
#: (click-odoo-contrib, every series up to 19). ``odoo-db``: ``odoo db
#: dump`` / ``odoo db load`` (Odoo 18 and later; the only tool on 20,
#: where click-odoo-contrib 1.24 refuses to run its database commands).
DB_TOOLS = ("click-odoo", "odoo-db")

#: How a copy is neutralized. ``odoo``: Odoo's own (``--neutralize``, from
#: 16.0). ``sql``: older series have no such thing, and
#: ``scripts/lib/common.sh`` turns off what would reach the outside.
NEUTRALIZERS = ("odoo", "sql")

#: The options that run Odoo with no server listening (``headless``).
HEADLESS_FLAGS = ("--no-http", "--no-xmlrpc")

#: The PostgreSQL majors the panel can run, oldest first. Not 9.6, which
#: doodba's template allows 8.0 to 12.0: it renders no ``backup`` service
#: for it, since its backup image does not support it (template v10.0.8),
#: and a series runs with every service of the panel or is not offered
#: (owner, 7-nov-2026).
POSTGRES_MAJORS = (
    "10", "11", "12", "13", "14", "15", "16", "17", "18",
)

_TEMPLATE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _card(
    offered, postgres, db_tool, template_min, neutralize="odoo",
    debian_archive=False, headless="--no-http", postgres_beyond_template=False,
    prefetch_repos=False,
):
    """Build one series' card, checking its values.

    :param bool offered: see the module docstring.
    :param tuple postgres: ``(lowest, highest, default)`` of
        ``POSTGRES_MAJORS``.
    :param str db_tool: one of ``DB_TOOLS``.
    :param template_min: ``vX.Y.Z`` or None.
    :param str neutralize: one of ``NEUTRALIZERS``.
    :param bool debian_archive: see the module docstring.
    :param str headless: one of ``HEADLESS_FLAGS``.
    :param bool postgres_beyond_template: see the module docstring.
    :param bool prefetch_repos: see the module docstring.
    :rtype: dict
    """
    lowest, highest, default = (POSTGRES_MAJORS.index(p) for p in postgres)
    if not lowest <= default <= highest:
        raise ValueError(f"PostgreSQL default outside its range: {postgres}")
    if db_tool not in DB_TOOLS:
        raise ValueError(f"unknown db tool {db_tool!r}")
    if neutralize not in NEUTRALIZERS:
        raise ValueError(f"unknown neutralizer {neutralize!r}")
    if headless not in HEADLESS_FLAGS:
        raise ValueError(f"unknown headless option {headless!r}")
    if template_min is not None and not _TEMPLATE_TAG_RE.match(template_min):
        raise ValueError(f"template_min must be a vX.Y.Z tag, got {template_min!r}")
    return {
        "offered": bool(offered),
        "postgres": tuple(postgres),
        "db_tool": db_tool,
        "neutralize": neutralize,
        "template_min": template_min,
        "debian_archive": bool(debian_archive),
        "headless": headless,
        "postgres_beyond_template": bool(postgres_beyond_template),
        "prefetch_repos": bool(prefetch_repos),
    }


# Ordered oldest → newest. The newest offered entry a panel's pin can
# render is the series a new project, or an instance outside one, gets
# (``default_version``); the platform's own panels name theirs.
VERSIONS = {
    # 7.0 to 17.0 are not offered at the launch (owner, 2-nov-2026): each
    # opens here once its own run of the series passes on the code that
    # will serve it. A record that holds one keeps it, and an administrator
    # can still deploy one, which is how that run tests it.
    # 7.0 is retired (owner, 7-nov-2026): the click-odoo of its image can
    # neither dump, restore nor update its database, so no panel deploys it
    # and the table no longer names it. No instance of production held it.
    # 8.0 to 15.0 run on what the doodba template allows each (v10.0.8's
    # copier.yml): 14 to 18, as they had, did not deploy (QA, 1-nov-2026).
    # Except below 12, the first major continuous recovery serves
    # (incubacloud_pitr's ``_pitr_postgres_supported``: pitr.sh recovers
    # with ``recovery.signal``), which 9.6 lacks as well as a backup
    # service (``POSTGRES_MAJORS``): 8.0 to 10.0, for which the template
    # allows 9.6 alone, run on 12 alone, 11.0 and 12.0 on 12 to 13, with 13
    # by default as the template recommends, and 13.0 to 15.0 from 12 up to
    # the template's latest (owner, 7-nov-2026; 8.0 to 12.0 each checked on
    # 12 that day: database created, modules installed and updated,
    # click-odoo and duplicity copies restored, login served, recovery to a
    # point in time; 13.0 to 15.0 are inside the template's own range, which
    # starts at 10, and are tried on 12 by the run of the series). 8.0 to
    # 10.0 are on Debian 8, gone from the mirrors, and have no
    # ``--no-http`` (7-nov-2026).
    "8.0": _card(
        False, ("12", "12", "12"), "click-odoo", None, "sql",
        debian_archive=True, headless="--no-xmlrpc",
        postgres_beyond_template=True, prefetch_repos=True,
    ),
    "9.0": _card(
        False, ("12", "12", "12"), "click-odoo", None, "sql",
        debian_archive=True, headless="--no-xmlrpc",
        postgres_beyond_template=True, prefetch_repos=True,
    ),
    "10.0": _card(
        False, ("12", "12", "12"), "click-odoo", None, "sql",
        debian_archive=True, headless="--no-xmlrpc",
        postgres_beyond_template=True, prefetch_repos=True,
    ),
    "11.0": _card(False, ("12", "13", "13"), "click-odoo", None, "sql"),
    "12.0": _card(False, ("12", "13", "13"), "click-odoo", None, "sql"),
    "13.0": _card(False, ("12", "16", "16"), "click-odoo", None, "sql"),
    "14.0": _card(False, ("12", "16", "16"), "click-odoo", None, "sql"),
    "15.0": _card(False, ("12", "17", "17"), "click-odoo", None, "sql"),
    "16.0": _card(False, ("14", "18", "17"), "click-odoo", None),
    "17.0": _card(False, ("14", "18", "17"), "click-odoo", None),
    "18.0": _card(True, ("14", "18", "17"), "click-odoo", None),
    "19.0": _card(True, ("14", "18", "17"), "click-odoo", None),
    # Offered since core 1.0.198 (plan Odoo 20, phase 2); the run of the
    # series (``qa-run -m versiones``) tests it with the rest. PostgreSQL 16
    # to 18, as the template requires; odoo db, since click-odoo-contrib
    # 1.24 refuses its database commands on 20 (lab of 22-oct-2026);
    # template v10.0.3, the first whose tasks also use odoo db on 20
    # (v10.0.2 renders it, but its ``invoke resetdb`` would fail in an
    # exported copy).
    "20.0": _card(True, ("16", "18", "17"), "odoo-db", "v10.0.3"),
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
    return POSTGRES_MAJORS[
        POSTGRES_MAJORS.index(lowest) : POSTGRES_MAJORS.index(highest) + 1
    ]


def copier_defaults(version):
    """Return what a copier run of *version* takes as its settings' defaults.

    For a card with ``postgres_beyond_template``, its range as the
    template's ``pg_per_odoo`` entry for the series, which the template's
    ``postgres_version`` question checks its answer against; keyed by the
    series as a number, as the template reads it. A question the template
    never asks (``when: false``) and keeps out of ``.copier-answers.yml``:
    given in the data, it reached ``copier copy`` but not the replay of
    the project's last render that ``copier update`` starts with, from
    that file alone, and the replay refused the PostgreSQL. A default of
    the run's settings file (``COPIER_SETTINGS_PATH``) reaches both
    (copier 9.19.1, 7-nov-2026). The executors write it next to the
    answers and ``ic_copier`` points copier at it.

    :param version: a series; empty or one the table does not know gets
        none, as ``headless_flag`` gives such a series its default.
    :return: ``{"pg_per_odoo": {8.0: {"oldest": "12", "latest": "12"}}}``,
        or an empty dict.
    :rtype: dict
    """
    if version not in VERSIONS or not card(version)["postgres_beyond_template"]:
        return {}
    lowest, highest, _default = card(version)["postgres"]
    return {"pg_per_odoo": {float(version): {"oldest": lowest, "latest": highest}}}


def postgres_default(version):
    """Return the PostgreSQL major a new instance of *version* gets.

    :param str version: a series.
    :rtype: str
    """
    return card(version)["postgres"][2]


def db_tool(version):
    """Return the database tool of *version*, one of ``DB_TOOLS``.

    :param str version: a series.
    :rtype: str
    """
    return card(version)["db_tool"]


def debian_archive(version):
    """Whether *version*'s image is built reading apt from Freexian's Extended LTS.

    :param str version: a series.
    :rtype: bool
    """
    return card(version)["debian_archive"]


def prefetch_repos(version):
    """Whether *version* fetches its repositories on the host before the build.

    :param str version: a series.
    :rtype: bool
    """
    return card(version)["prefetch_repos"]


def headless_flag(version):
    """Return the option that runs *version*'s Odoo with no server listening.

    :param version: a series; empty or one the table does not know
        counts as the newest series' option.
    :return: one of ``HEADLESS_FLAGS``.
    :rtype: str
    """
    if version not in VERSIONS:
        return HEADLESS_FLAGS[0]
    return card(version)["headless"]


def script_env(version):
    """Return the environment the operation scripts run with for *version*.

    What a script needs to know of the series and no operation passes
    by argument: the database tool (``IC_DB_TOOL``) and the way a copy is
    neutralized (``IC_NEUTRALIZE``), both ``scripts/lib/common.sh``. Handed to
    ``AbstractExecutor.run_script`` as ``env``; it reads the version
    alone, so an executor's test may describe its instance as plain data.

    :param str version: a series.
    :rtype: dict
    """
    return {
        "IC_DB_TOOL": db_tool(version),
        "IC_NEUTRALIZE": card(version)["neutralize"],
    }


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


#: The offered series, whatever a panel's pin, kept under the historical
#: name for its readers. The importer reads every series with a card and
#: asks ``may_choose`` of the one it finds.
ODOO_VERSIONS = offered_versions()

#: Ready-to-use value for ``fields.Selection(selection=...)``: every
#: series with a card, offered or not. An instance keeps a valid value
#: when its series stops being offered, and a series being prepared can
#: be deployed by QA before customers see it in a picker.
ODOO_VERSION_SELECTION = [(v, v) for v in VERSIONS]
