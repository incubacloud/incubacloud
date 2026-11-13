"""The card of each Odoo series, and that the panel reads from it.

``models/_odoo_versions.py`` is the one place a series is described.
These tests print the resolved table, so a change to it is read whole,
and check that what hangs off a card — the PostgreSQL picker and its
constraint, the database tool the scripts get, the template a deploy
needs, the series the importer refuses — comes from the card and nowhere else.
"""
from unittest.mock import MagicMock, patch

import yaml

from odoo import http as odoo_http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase, new_test_user

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_github,
    _routes_ops,
)
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud.github import import_inspector
from odoo.addons.incubacloud.github.client import GitHubAnonymousClient
from odoo.addons.incubacloud.models import _odoo_versions as versions
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.tests._template import pin_template
from odoo.addons.incubacloud.tests.test_cloud_security import CloudSecurityBase
from odoo.addons.incubacloud.tests.test_executor_features import (
    _make_deploy_executor,
    _make_rebuild_executor,
)
from odoo.addons.incubacloud.tests.test_github_import_limits import _blob, _entry
from odoo.addons.incubacloud.tests.test_refresh_from_production import (
    _argv,
    _find,
    _make_restore_executor,
)


class TestTheSeriesCards(BaseCase):
    """What the table says, read whole."""

    def test_the_resolved_table_reads_as_today(self):
        """Every series, written out: a change here is a change of policy.

        Printed on failure as the table a reader expects, so the diff
        says which series moved and in what.
        """
        def card(
            postgres, neutralize="odoo", db_tool="click-odoo", template=None,
            offered=False, debian_archive=False, headless="--no-http",
            beyond_template=False, prefetch_repos=True, image_git_safe=True,
        ):
            return {
                "offered": offered, "postgres": postgres, "db_tool": db_tool,
                "neutralize": neutralize, "template_min": template,
                "debian_archive": debian_archive, "headless": headless,
                "postgres_beyond_template": beyond_template,
                "prefetch_repos": prefetch_repos,
                "image_git_safe": image_git_safe,
            }

        expected = {
            # 7.0 is retired (7-nov-2026). What doodba's template
            # v10.0.8 allows each (its copier.yml) but below 12, the first
            # major with continuous recovery, which 9.6 lacks as well as a
            # backup service: 12 instead, beyond the template's 9.6 alone
            # for 8.0 to 10.0, and 12 to 13 for 11.0 and 12.0; 13.0 to
            # 15.0 start at 12 instead of the template's 10
            # (7-nov-2026). No neutralization of Odoo's own before 16.0.
            # 8.0 to 10.0 are built on Debian 8, gone from the mirrors,
            # and have no ``--no-http`` (7-nov-2026). Their git 2.1.4
            # follows a redirect to plain http with the token: no way back
            # to cloning in the image (8-nov-2026). Every series
            # fetches its repositories before the build: all but 19.0
            # from core 1.0.222, 19.0 too from 1.0.224.
            # Offered since 11-nov-2026 (8.0 since 13-nov-2026, its run of
            # the series having passed with the fix of Connect as user),
            # their run of the series having passed on the production
            # image of 9-nov-2026.
            **dict.fromkeys(
                ("8.0", "9.0", "10.0"),
                card(
                    ("12", "12", "12"), "sql", debian_archive=True,
                    headless="--no-xmlrpc", beyond_template=True,
                    image_git_safe=False, offered=True,
                ),
            ),
            **dict.fromkeys(
                ("11.0", "12.0"), card(("12", "13", "13"), "sql", offered=True),
            ),
            **dict.fromkeys(
                ("13.0", "14.0"), card(("12", "16", "16"), "sql", offered=True),
            ),
            "15.0": card(("12", "17", "17"), "sql", offered=True),
            # What the launch offered (2-nov-2026), and 16.0 and 17.0
            # since their run of the series passed (10-nov-2026).
            **dict.fromkeys(
                ("16.0", "17.0", "18.0", "19.0"),
                card(("14", "18", "17"), offered=True),
            ),
            # Offered since core 1.0.198: PostgreSQL 16 to 18, odoo db,
            # template v10.0.3 (lab of 22-oct-2026).
            "20.0": card(
                ("16", "18", "17"), db_tool="odoo-db", template="v10.0.3", offered=True,
            ),
        }
        self.assertEqual(dict(versions.VERSIONS), expected)

    def test_every_card_is_complete(self):
        for version, card in versions.VERSIONS.items():
            with self.subTest(version=version):
                self.assertEqual(
                    set(card),
                    {
                        "offered", "postgres", "db_tool", "neutralize",
                        "template_min", "debian_archive", "headless",
                        "postgres_beyond_template", "prefetch_repos",
                        "image_git_safe",
                    },
                )
                self.assertIn(card["db_tool"], versions.DB_TOOLS)
                self.assertIn(card["headless"], versions.HEADLESS_FLAGS)
                self.assertIn(card["neutralize"], versions.NEUTRALIZERS)
                lowest, highest, default = (
                    versions.POSTGRES_MAJORS.index(p) for p in card["postgres"]
                )
                self.assertLessEqual(lowest, default)
                self.assertLessEqual(default, highest)

    def test_every_series_fetches_its_repositories_first(self):
        """19.0 too, from core 1.0.224."""
        self.assertEqual(
            [v for v in versions.VERSIONS if not versions.prefetch_repos(v)],
            [],
        )

    def test_a_card_refuses_what_the_scripts_do_not_implement(self):
        with self.assertRaises(ValueError):
            versions._card(True, ("14", "18", "17"), "pg_dump", None)
        with self.assertRaises(ValueError):
            versions._card(True, ("16", "18", "15"), "click-odoo", None)
        with self.assertRaises(ValueError):
            versions._card(True, ("14", "18", "17"), "click-odoo", "10")
        with self.assertRaises(ValueError):
            versions._card(
                True, ("14", "18", "17"), "click-odoo", None, headless="--dev",
            )

    def test_the_field_knows_every_series_the_pickers_the_offered(self):
        """A series that stops being offered stays a valid value of the
        instances that run it; only the pickers and the importer drop it."""
        self.assertEqual(
            versions.ODOO_VERSION_SELECTION, [(v, v) for v in versions.VERSIONS],
        )
        self.assertEqual(versions.ODOO_VERSIONS, versions.offered_versions())

    def test_an_unknown_series_has_no_card(self):
        with self.assertRaises(KeyError):
            versions.card("6.1")

    def test_seven_is_retired_and_nine_six_gone(self):
        """7.0 can neither dump, restore nor update its database, and 9.6
        has no backup service (7-nov-2026): no picker, field or
        tool offers either."""
        self.assertNotIn("7.0", versions.VERSIONS)
        self.assertNotIn(("7.0", "7.0"), versions.ODOO_VERSION_SELECTION)
        self.assertNotIn("9.6", versions.POSTGRES_MAJORS)
        self.assertEqual(versions.POSTGRES_MAJORS[0], "10")

    def test_a_postgres_beyond_the_template_is_handed_to_copier(self):
        """8.0 to 10.0 on 12: the template's ``pg_per_odoo`` entry of the
        series, keyed as the template reads it; nothing for the rest."""
        for series in ("8.0", "9.0", "10.0"):
            with self.subTest(series=series):
                self.assertEqual(
                    versions.copier_defaults(series),
                    {"pg_per_odoo": {float(series): {"oldest": "12", "latest": "12"}}},
                )
        for series in ("11.0", "19.0", "20.0", "7.0", "", None, False):
            with self.subTest(series=series):
                self.assertEqual(versions.copier_defaults(series), {})


class TestCopierTakesThePostgresOfTheCard(BaseCase):
    """A deploy and a rebuild of 8.0 to 10.0 hand copier a settings file.

    Its defaults hold the card's PostgreSQL as the template's
    ``pg_per_odoo``: an answer in the data would reach ``copier copy`` but
    not the replay ``copier update`` starts with (copier 9.19.1,
    7-nov-2026). No other series gets one, and copier runs as before.
    """

    STEPS = (
        (_make_deploy_executor, "Deploy with copier"),
        (_make_rebuild_executor, "Update with copier"),
    )

    def test_the_copier_step_of_an_old_series_names_the_settings(self):
        for make, label in self.STEPS:
            with self.subTest(executor=make.__name__):
                ex = make(odoo_version="9.0")
                steps = dict(c[:2] for c in ex.get_commands())
                settings = ex._tmp("copier-settings.yml")
                self.assertTrue(
                    steps[label].startswith(f"IC_COPIER_SETTINGS={settings} bash "),
                    steps[label],
                )
                self.assertIn(settings, steps["Remove temp files"])
                self.assertEqual(
                    yaml.safe_load(ex._copier_settings_content()),
                    {
                        "defaults": {
                            "pg_per_odoo": {9.0: {"oldest": "12", "latest": "12"}}
                        }
                    },
                )

    def test_no_other_series_gets_one(self):
        for make, label in self.STEPS:
            for version in ("11.0", "19.0", "20.0", None):
                with self.subTest(executor=make.__name__, version=version):
                    ex = make(odoo_version=version)
                    steps = dict(c[:2] for c in ex.get_commands())
                    self.assertNotIn("IC_COPIER_SETTINGS", steps[label])
                    self.assertIsNone(ex._copier_settings_content())

    def test_the_postgres_range_and_default(self):
        self.assertEqual(
            versions.postgres_versions("19.0"), ("14", "15", "16", "17", "18"),
        )
        self.assertEqual(versions.postgres_default("19.0"), "17")

    def test_twenty_is_offered_with_its_own_postgres_and_tool(self):
        self.assertIn(("20.0", "20.0"), versions.ODOO_VERSION_SELECTION)
        self.assertEqual(versions.offered_versions()[-1], "20.0")
        self.assertEqual(versions.postgres_versions("20.0"), ("16", "17", "18"))
        self.assertEqual(
            versions.script_env("20.0"),
            {"IC_DB_TOOL": "odoo-db", "IC_NEUTRALIZE": "odoo"},
        )

    def test_the_pickers_get_each_offered_series(self):
        facts = versions.picker_facts()
        self.assertEqual(tuple(facts), versions.offered_versions())
        self.assertEqual(
            facts["19.0"],
            {"postgres": ["14", "15", "16", "17", "18"], "postgres_default": "17"},
        )
        self.assertEqual(
            facts["20.0"], {"postgres": ["16", "17", "18"], "postgres_default": "17"},
        )


class TestAnImageOnAnArchivedDebianBuilds(BaseCase):
    """8.0 to 10.0's image reads apt from Freexian's Extended LTS; no other's."""

    LABEL = "Read apt from the Debian archive"

    def _steps(self, make, version):
        """The steps of executor factory *make* for a *version* instance.

        :returns: ``{label: command}``, in order.
        :rtype: dict
        """
        return {c[0]: c[1] for c in make(odoo_version=version).get_commands()}

    def test_the_deploy_and_the_rebuild_of_an_old_series_get_it(self):
        for make in (_make_deploy_executor, _make_rebuild_executor):
            for version in ("8.0", "10.0"):
                with self.subTest(executor=make.__name__, version=version):
                    steps = self._steps(make, version)
                    self.assertIn("deploy.sh debian-archive", steps[self.LABEL])
                    labels = list(steps)
                    # In the build context before anything builds the image.
                    self.assertLess(
                        labels.index(self.LABEL),
                        labels.index("Install incubacloud_connect") + 2,
                    )
                    build = next(
                        i for i, label in enumerate(labels)
                        if label in ("Build Odoo image", "Rebuild Odoo image")
                    )
                    self.assertLess(labels.index(self.LABEL), build)

    def test_no_other_series_gets_it(self):
        for make in (_make_deploy_executor, _make_rebuild_executor):
            for version in ("7.0", "11.0", "15.0", "18.0", "19.0", "20.0", None):
                with self.subTest(executor=make.__name__, version=version):
                    self.assertNotIn(self.LABEL, self._steps(make, version))


class TestTheConnectModuleInstallsHeadless(BaseCase):
    """``restore.sh ensure-connect`` gets the series' headless option.

    The panel hands it from the card: Odoo 19's ``odoo --help`` lists the
    commands, not the server's options, so the script cannot read it
    there, and 10.0 refuses the ``--no-http`` of 11.0 and later.
    """

    LABEL = "Ensure incubacloud_connect"
    EXPECTED = (
        ("10.0", "--no-xmlrpc"), ("11.0", "--no-http"),
        ("18.0", "--no-http"), ("19.0", "--no-http"),
    )

    def _restore_argv(self, version):
        """The step's script arguments of a restore into a *version* instance.

        :rtype: list
        """
        ex = _make_restore_executor({"mode": "from_job"})
        ex.job.instance_id.odoo_version = version
        return _argv(_find(ex.get_commands(), self.LABEL))[2:]

    def _rebuild_argv(self, version):
        """The step's script arguments of a rebuild of a *version* instance.

        :rtype: list
        """
        cmds = _make_rebuild_executor(odoo_version=version).get_commands()
        return _argv(_find(cmds, self.LABEL))[2:]

    def test_a_restore_hands_the_series_option(self):
        for version, flag in self.EXPECTED:
            with self.subTest(version=version):
                self.assertEqual(
                    self._restore_argv(version),
                    ["ensure-connect", "~/projects/demo-inst", "odoo", "prod", flag],
                )

    def test_a_rebuild_hands_the_series_option(self):
        for version, flag in self.EXPECTED:
            with self.subTest(version=version):
                self.assertEqual(
                    self._rebuild_argv(version),
                    ["ensure-connect", "~/projects/test-inst", "odoo", "prod", flag],
                )

    def test_a_series_the_table_does_not_know_gets_the_newest_option(self):
        for version in (None, False, "", "6.1"):
            with self.subTest(version=version):
                self.assertEqual(versions.headless_flag(version), "--no-http")
        self.assertEqual(self._rebuild_argv(None)[-1], "--no-http")


class TestTheTemplateFloor(BaseCase):
    """``template_ref_allows``: a series' lowest template against a pin."""

    def test_a_series_with_no_floor_takes_any_pin(self):
        for ref in ("v9.7.0", "v10.0.8", "", None, "main"):
            with self.subTest(ref=ref):
                self.assertTrue(versions.template_ref_allows("19.0", ref))

    def test_a_floor_is_compared_as_a_version(self):
        """20.0 needs v10.0.3: compared as numbers, not as text."""
        for ref, expected in (
            ("v10.0.3", True), ("v10.0.8", True), ("v10.0.10", True),
            ("v11.0.0", True), ("10.1.0", True), ("v10.0.2", False),
            ("v9.7.0", False), ("v9.10.3", False),
            ("", True), ("main", True), (None, True),
        ):
            with self.subTest(ref=ref):
                self.assertIs(versions.template_ref_allows("20.0", ref), expected)


class TestAPanelOffersWhatItsPinRenders(BaseCase):
    """A panel offers, and defaults to, only what its template can deploy.

    Each panel keeps its own pin (``cloud.settings``): a panel installed
    with a core older than 1.0.195 (21-oct-2026) holds v9.6.1, below
    v10.0.3, 20.0's floor, unless someone changed it.
    """

    def test_production_s_pin_offers_20_and_defaults_to_it(self):
        self.assertEqual(versions.offered_versions("v10.0.8")[-1], "20.0")
        self.assertIn("20.0", versions.picker_facts("v10.0.8"))
        self.assertEqual(versions.default_version("v10.0.8"), "20.0")

    def test_a_pin_below_the_floor_leaves_20_out(self):
        for ref in ("v9.7.0", "v10.0.2"):
            with self.subTest(ref=ref):
                self.assertEqual(versions.offered_versions(ref)[-1], "19.0")
                self.assertNotIn("20.0", versions.picker_facts(ref))
                self.assertEqual(versions.default_version(ref), "19.0")

    def test_an_empty_pin_is_the_template_s_default_branch(self):
        """Newer than any tag, as ``template_ref_allows`` reads it: every
        offered series, the newest the default."""
        self.assertEqual(versions.offered_versions(""), versions.offered_versions())
        self.assertEqual(versions.default_version(""), "20.0")

    def test_with_no_pin_every_offered_series(self):
        """The importer's list, whatever a panel's pin."""
        self.assertEqual(versions.ODOO_VERSIONS, versions.offered_versions(None))
        self.assertEqual(versions.default_version(), "20.0")


class SeriesCardCase(TransactionCase):
    """An instance of an offered series, and one of the series being prepared.

    Created as the test's administrator, the only one who may choose a
    series that is not offered.
    """

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Series"})
        self.host = self.env["cloud.host"].create({
            "name": "series-host", "ip_address": "192.0.2.77", "user": "ubuntu",
            "wildcard_domain": "series.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "series", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        project20 = self.env["cloud.project"].create({
            "name": "Series 20", "odoo_version": "20.0",
        })
        self.instance20 = self.env["cloud.instance"].create({
            "name": "series20", "project_id": project20.id,
            "environment": "staging", "host_id": self.host.id,
            "odoo_version": "20.0",
        })


class TestWhatHangsOffTheCard(SeriesCardCase):

    def test_the_scripts_get_the_series_tool(self):
        self.assertEqual(
            versions.script_env(self.instance.odoo_version),
            {"IC_DB_TOOL": "click-odoo", "IC_NEUTRALIZE": "odoo"},
        )

    def test_a_postgres_outside_the_series_range_is_refused(self):
        """14 and 15 are choices of the field, and 20.0 does not run on them."""
        for major in ("14", "15"):
            with self.subTest(major=major), self.assertRaises(ValidationError):
                self.instance20.postgres_version = major
        self.instance20.postgres_version = "16"

    def test_a_new_instance_gets_the_postgres_of_its_series(self):
        """The field's default, 17, does not run 8.0 to 14.0."""
        old = self.env["cloud.instance"].create({
            "name": "series-eight", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "odoo_version": "8.0",
        })
        self.assertEqual(old.postgres_version, "12")

    def test_every_postgres_of_the_range_is_taken(self):
        for major in versions.postgres_versions("19.0"):
            with self.subTest(major=major):
                self.instance.postgres_version = major

    def test_todays_pin_renders_every_offered_series(self):
        for ref in ("v9.7.0", "v10.0.8", ""):
            with self.subTest(ref=ref):
                self.instance._check_template_ref(ref)
        # 20.0 from v10.0.3 on: production's pin, v10.0.8, and the
        # template's default branch.
        for ref in ("v10.0.8", ""):
            with self.subTest(ref=ref, version="20.0"):
                self.instance20._check_template_ref(ref)

    def test_a_pin_too_old_for_the_series_is_refused_with_the_runbook(self):
        """Production's v9.7.0 cannot render 20.0: refused before copier."""
        with self.assertRaises(UserError) as caught:
            self.instance20._check_template_ref("v9.7.0")
        self.assertIn("v10.0.3", str(caught.exception))
        self.assertIn("RB-15", str(caught.exception))
        self.instance20._check_template_ref("v10.0.8")

    def test_a_series_before_sixteen_is_neutralized_by_the_panel(self):
        for series in ("8.0", "15.0"):
            with self.subTest(series=series):
                self.assertEqual(
                    versions.script_env(series),
                    {"IC_DB_TOOL": "click-odoo", "IC_NEUTRALIZE": "sql"},
                )

    def test_every_series_gets_its_backup_service(self):
        """12, the PostgreSQL of 8.0 to 10.0, has the template's backup
        service, as every other major the table names: an instance with a
        backend counts on it."""
        backend = self.env["cloud.backup.backend"].create({
            "name": "series-backend", "backend_type": "s3",
            "s3_bucket": "series-bucket", "s3_access_key_id": "AKIASERIES",
            "s3_secret_access_key": "shh",
        })
        for series in ("8.0", "10.0", "11.0", "19.0"):
            with self.subTest(series=series):
                # One production per project.
                project = self.env["cloud.project"].create({
                    "name": f"Series {series}", "odoo_version": series,
                })
                inst = self.env["cloud.instance"].create({
                    "name": f"series-prod-{series[:-2]}",
                    "project_id": project.id, "environment": "production",
                    "host_id": self.host.id, "odoo_version": series,
                    "backup_backend_id": backend.id,
                })
                self.assertTrue(inst._backup_enabled())
                self.assertIn("backup", inst.expected_services())
                answers = inst._render_copier_answers()
                self.assertTrue(answers["backup_dst"])
                self.assertEqual(answers["postgres_version"], inst.postgres_version)

    def test_a_series_on_odoo_db_gets_it(self):
        self.assertEqual(
            versions.script_env(self.instance20.odoo_version),
            {"IC_DB_TOOL": "odoo-db", "IC_NEUTRALIZE": "odoo"},
        )

    def test_the_deploy_and_the_rebuild_ask_before_uploading(self):
        """The check is the first thing both do with the instance."""
        import inspect
        for cls in (DeployInstanceExecutor, RebuildInstanceExecutor):
            with self.subTest(executor=cls.__name__):
                source = inspect.getsource(cls.before_execute)
                check = source.index("_check_template_ref(self._copier_template()[1])")
                self.assertLess(check, source.index("_preflight_tls_check"))
                self.assertLess(check, source.index("_upload_copier_files"))

    def test_the_importer_reads_every_series_it_knows(self):
        """A repository of a series not offered is read as that series,
        not as none: read as none, its project took the default, 20.0,
        and its code was deployed on a series it was not written for.
        Whether the import may take it is the route's to say. A manifest
        names a series only by a version of at least four parts; the
        answers, like a branch, by any ``X.0``, so that a series the panel
        has no card for is refused as not offered."""
        self.assertEqual(import_inspector._KNOWN_SERIES, set(versions.VERSIONS))
        manifest = import_inspector.version_from_manifest_text
        self.assertEqual(manifest("{'version': '16.0.1.0.0'}"), "16.0")
        self.assertEqual(manifest("{'version': '17.0.1.0.0'}"), "17.0")
        self.assertEqual(manifest("{'version': '19.0.1.0.0'}"), "19.0")
        self.assertEqual(manifest("{'version': '20.0.1.0.0'}"), "20.0")
        self.assertEqual(manifest("{'version': '8.0.1.0'}"), "8.0")
        self.assertEqual(manifest("{'version': '21.0.1.0.0'}"), "")
        for version in ("1.0", "12.0", "8.0.1", "17.0.1"):
            with self.subTest(version=version):
                self.assertEqual(manifest(f"{{'version': '{version}'}}"), "")
        answers = import_inspector._parse_copier_answers
        self.assertEqual(answers('odoo_version: "17.0"\n')["odoo_version"], "17.0")
        self.assertEqual(answers('odoo_version: "19.0"\n')["odoo_version"], "19.0")
        self.assertEqual(answers('odoo_version: "7.0"\n')["odoo_version"], "7.0")
        self.assertEqual(answers('odoo_version: "21.0"\n')["odoo_version"], "21.0")
        self.assertEqual(answers('odoo_version: "8.1"\n')["odoo_version"], "")


class TestANewRecordGetsTheNewestSeriesItsPanelOffers(TransactionCase):
    """What creates a project, or an instance outside a project, without
    naming a series gets the newest one offered that the panel's pin
    renders (22-oct-2026). An instance in a project
    takes the project's."""

    def _new_project_and_instance(self, ref):
        """A project and an instance, created without a series under pin *ref*.

        :param str ref: the panel's doodba template pin.
        :return: ``(project, instance)``; the instance has no project, so
            its series is the field's default too.
        """
        pin_template(self.env, ref)
        project = self.env["cloud.project"].create({"name": f"Default {ref}"})
        instance = self.env["cloud.instance"].create({
            "name": "default", "environment": "staging",
        })
        return project, instance

    def test_production_s_pin_gives_20(self):
        project, instance = self._new_project_and_instance("v10.0.8")
        self.assertEqual(project.odoo_version, "20.0")
        self.assertEqual(instance.odoo_version, "20.0")

    def test_a_pin_below_20_s_floor_gives_19(self):
        project, instance = self._new_project_and_instance("v9.7.0")
        self.assertEqual(project.odoo_version, "19.0")
        self.assertEqual(instance.odoo_version, "19.0")

    def test_an_empty_pin_gives_20(self):
        project, instance = self._new_project_and_instance("")
        self.assertEqual(project.odoo_version, "20.0")
        self.assertEqual(instance.odoo_version, "20.0")

    def test_the_panel_s_new_instance_takes_its_project_s_series(self):
        """``/cloud/create_instance`` gives it the project's, not the default."""
        pin_template(self.env, "v10.0.8")
        project = self.env["cloud.project"].create(
            {"name": "On 19", "odoo_version": "19.0"},
        )
        host = self.env["cloud.host"].create({
            "name": "default-host", "ip_address": "192.0.2.78", "user": "ubuntu",
            "wildcard_domain": "default.example.com",
        })
        controller = _routes_crud.CrudMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env
        with patch.object(_routes_crud, "request", fake_request):
            answer = controller.cloud_create_instance({
                "name": "follows", "project_id": project.id,
                "environment": "production", "host_id": host.id,
                "odoo_version": 20.0,
            })
        instance = self.env["cloud.instance"].browse(answer["id"])
        self.assertEqual(instance.odoo_version, "19.0")

    def test_a_host_import_into_a_project_takes_its_series(self):
        """``/cloud/import_host_instance`` into an existing project, from
        copier answers that name no series: the project's, not the
        default, or its next rebuild would render 20.0."""
        pin_template(self.env, "v10.0.8")
        project = self.env["cloud.project"].create(
            {"name": "T7 import on 19", "odoo_version": "19.0"},
        )
        host = self.env["cloud.host"].create({
            "name": "import-host", "ip_address": "192.0.2.79", "user": "ubuntu",
            "wildcard_domain": "import.example.com",
        })
        controller = _routes_ops.OpsMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env
        # The host's answers to the route's 13 reads: a .copier-answers.yml
        # that names the project and no series, and nothing else.
        reads = [("project_name: T7 import on 19\n", "")] + [("", "")] * 12
        with patch.object(_routes_ops, "request", fake_request), patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ):
            answer = controller.cloud_import_host_instance(host.id, "/srv/on19")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["project_id"], project.id)
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertEqual(instance.odoo_version, "19.0")


class TestThePickersRoute(TransactionCase):
    """``/cloud/get_odoo_versions``: what the SPA's pickers are served."""

    def _serve(self, route, **kwargs):
        """Call *route* of the panel's controller, as a request would.

        :param str route: the controller method's name.
        :param kwargs: the route's arguments.
        :return: what it answers.
        """
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env
        controller = _routes_crud.CrudMixin()
        with patch.object(_routes_crud, "request", fake_request):
            return getattr(controller, route)(**kwargs)

    def test_it_serves_the_offered_series_and_their_cards(self):
        """Not the field's whole selection: a series being prepared, or
        one no longer offered, is a valid value and still not offered."""
        pin_template(self.env, "v10.0.8")
        answer = self._serve("cloud_get_odoo_versions")
        self.assertEqual(answer["odoo_versions"], list(versions.offered_versions()))
        self.assertEqual(answer["facts"], versions.picker_facts())

    def test_a_panel_pinned_below_20_s_floor_does_not_serve_it(self):
        """The SPA's new project form falls back to the last one served."""
        pin_template(self.env, "v9.7.0")
        answer = self._serve("cloud_get_odoo_versions")
        self.assertEqual(answer["odoo_versions"][-1], "19.0")
        self.assertNotIn("20.0", answer["facts"])

    def test_the_new_project_form_opens_on_the_default(self):
        for ref, expected in (("v10.0.8", "20.0"), ("v9.7.0", "19.0")):
            with self.subTest(ref=ref):
                pin_template(self.env, ref)
                answer = self._serve("cloud_get_tags", scope="project")
                self.assertEqual(
                    answer["project_defaults"]["odoo_version"], expected,
                )

    def test_the_new_project_form_opens_on_the_model_s_author_and_licence(self):
        """The form's author and licence are the fields' defaults, which a
        layer may change: the form does not write them itself."""
        defaults = self._serve("cloud_get_tags", scope="project")["project_defaults"]
        expected = self.env["cloud.project"].default_get(
            ["project_author", "project_license"],
        )
        self.assertEqual(defaults["project_author"], expected["project_author"])
        self.assertEqual(defaults["project_license"], expected["project_license"])


class TestOnlyAnAdministratorChoosesASeriesNotOffered(CloudSecurityBase):
    """``may_choose``: the pickers hide a series, and RPC cannot reach it.

    A manager of the panel is not an Odoo administrator; on a customer's
    own panel that group is ours alone, so a customer cannot deploy a
    series before its QA passed.

    Every series is offered today, so 20.0 is held back here as it was
    until core 1.0.198.
    """

    def setUp(self):
        super().setUp()
        self.startPatcher(patch.dict(versions.VERSIONS, {
            "20.0": versions._card(False, ("16", "18", "17"), "odoo-db", "v10.0.3"),
        }))
        self.manager = self._create_user("series_manager", "group_cloud_manager")
        self.assertFalse(self.manager.has_group("base.group_system"))

    def test_a_manager_cannot_create_a_project_of_it(self):
        Project = self.env["cloud.project"].with_user(self.manager)
        with self.assertRaises(ValidationError) as caught:
            Project.create({"name": "Too soon", "odoo_version": "20.0"})
        offered = ", ".join(versions.offered_versions(
            versions.pinned_template_ref(self.env),
        ))
        self.assertEqual(
            str(caught.exception),
            f"Odoo 20.0 is not offered yet. This panel offers: {offered}.",
        )
        self.assertNotIn("20.0", offered)
        Project.create({"name": "Fine", "odoo_version": "19.0"})

    def test_a_manager_cannot_move_an_instance_to_it(self):
        project = self._create_project(members=[self.manager])
        instance = self._create_instance(project)
        with self.assertRaises(ValidationError):
            instance.with_user(self.manager).odoo_version = "20.0"

    def test_an_administrator_can(self):
        """As QA does, to deploy the series before it is offered."""
        project = self.env["cloud.project"].create(
            {"name": "QA 20", "odoo_version": "20.0"},
        )
        self.assertEqual(project.odoo_version, "20.0")


class TestOnlyAnAdministratorChoosesASeriesItsPinCannotRender(CloudSecurityBase):
    """``may_choose`` reads the panel's pin: an offered series the panel
    cannot deploy is not offered there."""

    def setUp(self):
        super().setUp()
        self.manager = self._create_user("pin_manager", "group_cloud_manager")
        self.assertFalse(self.manager.has_group("base.group_system"))

    def test_below_the_floor_a_manager_cannot_choose_it(self):
        """Told it is not offered on this panel: the series is offered,
        the panel's pin cannot render it."""
        pin_template(self.env, "v9.7.0")
        Project = self.env["cloud.project"].with_user(self.manager)
        with self.assertRaises(ValidationError) as caught:
            Project.create({"name": "Too old a pin", "odoo_version": "20.0"})
        self.assertTrue(
            str(caught.exception).startswith(
                "Odoo 20.0 is not offered on this panel. This panel offers: ",
            ),
            caught.exception,
        )
        project = Project.create({"name": "Default"})
        self.assertEqual(project.odoo_version, "19.0")

    def test_after_a_rollback_a_20_record_is_saved_with_its_series(self):
        """The forms send the series back with every save: a pin rolled
        back below 20.0's floor (RB-15) must not lock a manager out of the
        20.0 projects and instances, while a record moved to 20.0 is still
        refused."""
        pin_template(self.env, "v10.0.8")
        project20 = self._create_project("On 20", members=[self.manager])
        instance20 = self._create_instance(project20)
        Project = self.env["cloud.project"]
        project19 = Project.create({"name": "On 19", "odoo_version": "19.0"})
        instance19 = self._create_instance(project19)
        # Moved along with a 20.0 one: not every record holds it already.
        other19 = Project.create({"name": "Also on 19", "odoo_version": "19.0"})
        self.assertEqual(project20.odoo_version, "20.0")
        self.assertEqual(instance20.odoo_version, "20.0")
        pin_template(self.env, "v9.7.0")
        project20.with_user(self.manager).write(
            {"name": "Still on 20", "odoo_version": "20.0"},
        )
        instance20.with_user(self.manager).write(
            {"odoo_conf": "workers = 2", "odoo_version": "20.0"},
        )
        self.assertEqual(project20.name, "Still on 20")
        self.assertEqual(instance20.odoo_conf, "workers = 2")
        for records in (project19, instance19, other19 | project20):
            with self.subTest(records=records), self.assertRaises(
                ValidationError,
            ):
                records.with_user(self.manager).write({"odoo_version": "20.0"})

    def test_a_project_manager_s_new_records_read_the_pin(self):
        """As the system: a project manager may not read the settings,
        and the default and the check of a new record read its pin."""
        pm = self._create_user("pin_pm", "group_cloud_project_manager")
        with self.assertRaises(AccessError):
            self.env["cloud.settings"].with_user(pm)._get()
        for ref, expected in (("v9.7.0", "19.0"), ("v10.0.8", "20.0")):
            with self.subTest(ref=ref):
                pin_template(self.env, ref)
                project = self.env["cloud.project"].with_user(pm).create(
                    {"name": f"PM {ref}"},
                )
                self.assertEqual(project.odoo_version, expected)
                instance = self.env["cloud.instance"].with_user(pm).create({
                    "name": "pm", "project_id": project.id,
                    "environment": "staging",
                })
                self.assertEqual(instance.odoo_version, expected)

    def test_from_the_floor_a_manager_can(self):
        pin_template(self.env, "v10.0.3")
        Project = self.env["cloud.project"].with_user(self.manager)
        self.assertEqual(Project.create({"name": "Default"}).odoo_version, "20.0")

    def test_an_administrator_still_can_and_its_deploy_is_refused(self):
        """As today: the deploy refuses before copier, naming RB-15."""
        pin_template(self.env, "v9.7.0")
        project = self.env["cloud.project"].create(
            {"name": "Admin 20", "odoo_version": "20.0"},
        )
        instance = self._create_instance(project, odoo_version="20.0")
        with self.assertRaises(UserError):
            instance._check_template_ref("v9.7.0")


class TestAnImportTakesTheSeriesOfItsCode(CloudSecurityBase):
    """An import gives its project the series its code is for, or stops.

    Before core 1.0.218 the GitHub importer read a series it did not
    offer as none, and a project imported from an Odoo.sh repository on
    17.0 was created on 20.0 without a word; a host's instance whose
    answers named no series was too. Now the import refuses, naming the
    series it found and the ones the panel offers, before it creates
    anything (``import_series_refusal``).
    """

    def setUp(self):
        super().setUp()
        # Every series is offered today, so 8.0 is held back here as it
        # was until core 1.0.228.
        self.startPatcher(patch.dict(
            versions.VERSIONS, {"8.0": {**versions.VERSIONS["8.0"], "offered": False}},
        ))
        pin_template(self.env, "v10.0.8")
        self.offered = ", ".join(versions.offered_versions("v10.0.8"))
        self.manager = self._create_user("import_manager", "group_cloud_manager")
        self.assertFalse(self.manager.has_group("base.group_system"))

    def _request(self, env):
        """A request of *env*, bound where the import routes read it.

        :param env: the environment the request runs as.
        :return: the patchers' context managers, to enter together.
        """
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        return (
            patch.object(_routes_github, "request", fake_request),
            patch.object(_routes_ops, "request", fake_request),
            patch.object(odoo_http, "request", fake_request),
        )

    def _import_from_github(self, env, series, name, repos_data=()):
        """Create what the GitHub importer would from a repository of *series*.

        :param env: the environment the import runs as.
        :param str series: what the inspector read; empty for none.
        :param str name: the repository's name, the project's.
        :param repos_data: the repository lines the inspector read.
        :return: what the route answers.
        """
        inspected = {
            "ok": True, "repo_name": name, "repo_type": "odoosh",
            "repos_data": list(repos_data), "copier": {}, "pip_deps": "",
            "apt_deps": "", "odoo_conf": "", "odoo_version": series,
            "submodules": [],
        }
        github, ops, http = self._request(env)
        with github, ops, http:
            return CloudDataLoadController()._create_bounded_github_import(
                inspected,
            )

    def _import_from_doodba(self, env, copier_answers, name, branch="main"):
        """Inspect a Doodba repository, then import what the inspector read.

        The inspector reads a served tree (its ``.copier-answers.yml`` and
        a ``repos.yaml`` of one repository line) and the route creates
        from what it found, as ``/cloud/import_project`` does.

        :param env: the environment the import runs as.
        :param str copier_answers: the ``.copier-answers.yml``'s text.
        :param str name: the repository's name.
        :param str branch: the branch imported.
        :return: what the route answers.
        """
        files = {
            ".copier-answers.yml": copier_answers,
            "odoo/custom/src/repos.yaml": (
                "./odoo: {}\ncustom:\n  remotes:\n"
                "    origin: https://github.com/acme/addons.git\n"
                "  target: origin 19.0\n"
            ),
        }
        entries = list(map(_entry, files, files.values()))
        blobs = {e["sha"]: _blob(files[e["path"]]) for e in entries}
        client = MagicMock(spec=GitHubAnonymousClient)

        def _get(endpoint, *, budget=None, max_bytes=None):
            """Serve the repository's metadata, branch, tree and blobs."""
            if endpoint == f"/repos/acme/{name}":
                return {"default_branch": "main"}
            if endpoint == f"/repos/acme/{name}/branches/{branch}":
                return {"commit": {"sha": "a" * 40}}
            if "/git/trees/" in endpoint:
                return {"tree": entries, "truncated": False}
            if "/git/blobs/" in endpoint:
                return blobs[endpoint.rsplit("/", 1)[1]]
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        client.get.side_effect = _get
        inspected = import_inspector.GitHubImportInspector([client]).inspect_import(
            f"https://github.com/acme/{name}", branch,
        )
        self.assertEqual(inspected["repo_type"], "doodba")
        self.assertEqual(len(inspected["repos_data"]), 1)
        github, ops, http = self._request(env)
        with github, ops, http:
            return CloudDataLoadController()._create_bounded_github_import(
                inspected,
            )

    def _import_from_host(self, env, copier_answers, path, repos_yaml=""):
        """Import a host's instance whose ``.copier-answers.yml`` is *copier_answers*.

        :param env: the environment the import runs as.
        :param str copier_answers: the file's text; empty for none.
        :param str path: the instance's directory.
        :param str repos_yaml: the host's ``repos.yaml``; empty for none.
        :return: what the route answers.
        """
        host = self.env["cloud.host"].create({
            "name": f"host-{path.strip('/').replace('/', '-')}",
            "ip_address": "192.0.2.81", "user": "ubuntu",
            "wildcard_domain": "series-import.example.com",
        })
        controller = _routes_ops.OpsMixin()
        controller._sec = lambda: env["cloud.security.mixin"]
        # The host's answers to the route's 13 reads.
        reads = [(copier_answers, ""), (repos_yaml, "")] + [("", "")] * 11
        github, ops, http = self._request(env)
        with github, ops, http, patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ):
            return controller.cloud_import_host_instance(host.id, path)

    _REPOS_YAML = (
        "addons:\n  remotes:\n    origin: https://github.com/acme/addons.git\n"
        "  target: origin 19.0\n"
    )

    def _refusal(self, series):
        """The refusal of a series the panel does not offer.

        :param str series: the series the code is for.
        :return: the answer a route gives, as a dict.
        """
        return {
            "ok": False,
            "error": f"The code being imported is for Odoo {series}, which this "
                     f"panel does not offer. This panel offers: {self.offered}.",
        }

    def _assert_nothing_created(self, name):
        """Assert a refused or failed import left no project of that name.

        Nor a repository line of the project's, nor an instance in it.

        :param str name: the project's name.
        """
        project = self.env["cloud.project"].search([("name", "=", name)])
        self.assertFalse(project)
        self.assertFalse(self.env["cloud.project.repo"].search(
            [("url", "=", "https://github.com/acme/addons.git"),
             ("project_id.name", "=", name)],
        ))
        self.assertFalse(self.env["cloud.instance"].search(
            [("project_id.name", "=", name)],
        ))

    def test_a_repository_on_8_is_refused_naming_both(self):
        """A manager importing an Odoo.sh repository of 8.0, which the
        panel does not offer, is told so and gets no project."""
        answer = self._import_from_github(
            self.env(user=self.manager), "8.0", "odoosh-8"
        )
        self.assertEqual(answer, self._refusal("8.0"))
        self._assert_nothing_created("odoosh-8")

    def test_a_repository_that_names_no_series_is_refused(self):
        """Not given the default: nothing says its code runs on it."""
        answer = self._import_from_github(
            self.env(user=self.manager), "", "odoosh-none"
        )
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["error"].startswith(
            "Could not tell which Odoo series this repository is for.",
        ), answer)
        self.assertIn(".copier-answers.yml", answer["error"])
        self.assertTrue(answer["error"].endswith(
            f"This panel offers: {self.offered}.",
        ), answer)
        self._assert_nothing_created("odoosh-none")

    def test_a_repository_of_an_offered_series_takes_it(self):
        """A manager, not an administrator, imports a repository of 19.0:
        project and production instance are made on it."""
        answer = self._import_from_github(
            self.env(user=self.manager), "19.0", "odoosh-19"
        )
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertEqual(project.odoo_version, "19.0")
        self.assertEqual(instance.odoo_version, "19.0")

    def test_an_administrator_imports_a_series_not_offered(self):
        """As the project form lets him (``may_choose``): a user of the Odoo
        administrators' group with a cloud role, not the superuser."""
        admin = new_test_user(
            self.env, login="import_admin",
            groups="base.group_user,base.group_system,"
                   "incubacloud.group_cloud_manager",
        )
        answer = self._import_from_github(
            self.env(user=admin), "8.0", "odoosh-8-admin",
        )
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        self.assertEqual(project.odoo_version, "8.0")

    def test_a_series_without_a_card_is_refused_even_to_an_administrator(self):
        """7.0 has no card, so no panel deploys it: the administrator's
        privilege covers the series with one, not this."""
        for series in ("7.0", "21.0"):
            with self.subTest(series=series):
                answer = self._import_from_github(
                    self.env, series, f"odoosh-{series}",
                )
                self.assertEqual(answer, self._refusal(series))
                self._assert_nothing_created(f"odoosh-{series}")
        answer = self._import_from_host(
            self.env, "odoo_version: 7.0\nproject_name: Host on 7\n",
            "/srv/on7", self._REPOS_YAML,
        )
        self.assertEqual(answer, self._refusal("7.0"))
        self._assert_nothing_created("Host on 7")

    def test_a_doodba_repository_is_inspected_and_refused_end_to_end(self):
        """From the served tree to the route: the answers' 8.0 is read
        and refused for a manager, with the repository line it would
        have made; an administrator's 7.0, too."""
        manager = self.env(user=self.manager)
        answer = self._import_from_doodba(
            manager, "odoo_version: '8.0'\nproject_name: Doodba 8\n", "doodba8",
        )
        self.assertEqual(answer, self._refusal("8.0"))
        self._assert_nothing_created("Doodba 8")
        answer = self._import_from_doodba(
            self.env, "odoo_version: '7.0'\nproject_name: Doodba 7\n", "doodba7",
        )
        self.assertEqual(answer, self._refusal("7.0"))
        self._assert_nothing_created("Doodba 7")

    def test_a_doodba_repository_naming_no_series_is_refused_naming_its_answers(self):
        """The branch is ``main`` and the answers name none: the message
        points to ``odoo_version`` in the answers."""
        answer = self._import_from_doodba(
            self.env(user=self.manager), "project_name: Doodba none\n", "doodbanone",
        )
        self.assertFalse(answer["ok"])
        self.assertIn("odoo_version in its .copier-answers.yml", answer["error"])
        self._assert_nothing_created("Doodba none")

    def test_a_doodba_repository_of_an_offered_series_takes_it_with_its_lines(self):
        """The same path on 19.0 creates the project, its line and instance."""
        answer = self._import_from_doodba(
            self.env(user=self.manager),
            "odoo_version: '19.0'\nproject_name: Doodba 19\n", "doodba19",
        )
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        self.assertEqual(project.odoo_version, "19.0")
        self.assertEqual(
            project.repo_ids.mapped("url"), ["https://github.com/acme/addons.git"],
        )

    def test_a_host_s_instance_on_8_is_refused_naming_both(self):
        """A manager importing a host's instance whose answers say 8.0."""
        answer = self._import_from_host(
            self.env(user=self.manager),
            "odoo_version: 8.0\nproject_name: Host on 8\n",
            "/srv/on8", self._REPOS_YAML,
        )
        self.assertEqual(answer, self._refusal("8.0"))
        self._assert_nothing_created("Host on 8")

    def test_a_host_s_instance_that_names_no_series_is_refused(self):
        """Outside a project: inside one it takes the project's
        (``test_a_host_import_into_a_project_takes_its_series``)."""
        answer = self._import_from_host(
            self.env(user=self.manager), "project_name: Host on nothing\n",
            "/srv/onnone", self._REPOS_YAML,
        )
        self.assertEqual(answer, {
            "ok": False,
            "error": "Could not tell which Odoo series this instance runs: its "
                     ".copier-answers.yml names none. This panel offers: "
                     f"{self.offered}.",
        })
        self._assert_nothing_created("Host on nothing")

    def test_a_host_s_instance_into_a_project_not_offered_is_refused(self):
        """Answers that name none take the existing project's series; if it
        is one the panel does not offer (an administrator made it), a
        manager's import is refused, not given an instance on it."""
        project = self.env["cloud.project"].create(
            {"name": "Held on 8", "odoo_version": "8.0"},
        )
        answer = self._import_from_host(
            self.env(user=self.manager), "project_name: Held on 8\n", "/srv/held8",
        )
        self.assertEqual(answer, self._refusal("8.0"))
        self.assertFalse(project.instance_ids)

    def test_a_series_that_reads_as_none_is_said(self):
        """``odoo_version: abc`` is told, and not dropped with the rest of
        the answers; an existing project's series does not stand in."""
        project = self.env["cloud.project"].create(
            {"name": "Held on 19", "odoo_version": "19.0"},
        )
        for name in ("Held on 19", "Not held"):
            with self.subTest(name=name):
                answer = self._import_from_host(
                    self.env(user=self.manager),
                    f"odoo_version: abc\nproject_name: {name}\n",
                    f"/srv/{name.replace(' ', '-')}", self._REPOS_YAML,
                )
                self.assertEqual(answer, {
                    "ok": False,
                    "error": "The series of .copier-answers.yml is not "
                             "understood: abc. This panel offers: "
                             f"{self.offered}.",
                })
        self.assertFalse(project.instance_ids)
        self._assert_nothing_created("Not held")

    def test_a_domain_another_instance_holds_stops_the_import_before_it_creates(self):
        """The route answers ``ok: False`` and the request's transaction is
        kept: the project and its lines must not be created before."""
        holder = self._create_instance(self._create_project("Holder"))
        self.env["cloud.instance.domain"].create(
            {"instance_id": holder.id, "hostname": "taken.series-import.example.com"},
        )
        answer = self._import_from_host(
            self.env(user=self.manager),
            "odoo_version: 19.0\nproject_name: Wants the domain\n"
            "domains_prod:\n  - hosts: [taken.series-import.example.com]\n",
            "/srv/wants", self._REPOS_YAML,
        )
        self.assertFalse(answer["ok"])
        self.assertIn("taken.series-import.example.com", answer["error"])
        self._assert_nothing_created("Wants the domain")

    def test_an_instance_that_cannot_be_created_leaves_no_project(self):
        """The same, when the instance's creation fails after the project
        and its lines were made: they are rolled back with it."""
        with patch.object(
            self.env.registry["cloud.instance"], "create", autospec=True,
            side_effect=ValueError("no instance"),
        ), self.assertLogs(_routes_ops._logger.name, level="ERROR"):
            answer = self._import_from_host(
                self.env(user=self.manager),
                "odoo_version: 19.0\nproject_name: Fails late\n",
                "/srv/fails", self._REPOS_YAML,
            )
        self.assertEqual(answer, {
            "ok": False,
            "error": "An internal error occurred. Check server logs.",
        })
        self._assert_nothing_created("Fails late")
