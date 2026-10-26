"""The card of each Odoo series, and that the panel reads from it.

``models/_odoo_versions.py`` is the one place a series is described.
These tests print the resolved table, so a change to it is read whole,
and check that what hangs off a card — the PostgreSQL picker and its
constraint, the database tool the scripts get, the template a deploy
needs, the importer's acceptance — comes from the card and nowhere else.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud, _routes_ops
from odoo.addons.incubacloud.github import import_inspector
from odoo.addons.incubacloud.models import _odoo_versions as versions
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.tests._template import pin_template
from odoo.addons.incubacloud.tests.test_cloud_security import CloudSecurityBase


class TestTheSeriesCards(BaseCase):
    """What the table says, read whole."""

    def test_the_resolved_table_reads_as_today(self):
        """Every series, written out: a change here is a change of policy.

        Printed on failure as the table a reader expects, so the diff
        says which series moved and in what.
        """
        expected = {
            v: {
                "offered": True, "postgres": (14, 18, 17),
                "db_tool": "click-odoo", "template_min": None,
            }
            for v in (
                "7.0", "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
                "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
            )
        }
        # Offered since core 1.0.198: PostgreSQL 16 to 18, odoo db,
        # template v10.0.3 (plan Odoo 20, lab of 22-oct-2026).
        expected["20.0"] = {
            "offered": True, "postgres": (16, 18, 17),
            "db_tool": "odoo-db", "template_min": "v10.0.3",
        }
        self.assertEqual(dict(versions.VERSIONS), expected)

    def test_every_card_is_complete(self):
        for version, card in versions.VERSIONS.items():
            with self.subTest(version=version):
                self.assertEqual(
                    set(card), {"offered", "postgres", "db_tool", "template_min"},
                )
                self.assertIn(card["db_tool"], versions.DB_TOOLS)
                lowest, highest, default = card["postgres"]
                self.assertLessEqual(lowest, default)
                self.assertLessEqual(default, highest)

    def test_a_card_refuses_what_the_scripts_do_not_implement(self):
        with self.assertRaises(ValueError):
            versions._card(True, (14, 18, 17), "pg_dump", None)
        with self.assertRaises(ValueError):
            versions._card(True, (16, 18, 15), "click-odoo", None)
        with self.assertRaises(ValueError):
            versions._card(True, (14, 18, 17), "click-odoo", "10")

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

    def test_the_postgres_range_and_default(self):
        self.assertEqual(
            versions.postgres_versions("19.0"), ("14", "15", "16", "17", "18"),
        )
        self.assertEqual(versions.postgres_default("19.0"), "17")

    def test_twenty_is_offered_with_its_own_postgres_and_tool(self):
        self.assertIn(("20.0", "20.0"), versions.ODOO_VERSION_SELECTION)
        self.assertEqual(versions.offered_versions()[-1], "20.0")
        self.assertEqual(versions.postgres_versions("20.0"), ("16", "17", "18"))
        self.assertEqual(versions.script_env("20.0"), {"IC_DB_TOOL": "odoo-db"})

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
            {"IC_DB_TOOL": "click-odoo"},
        )

    def test_a_postgres_outside_the_series_range_is_refused(self):
        """14 and 15 are choices of the field, and 20.0 does not run on them."""
        for major in ("14", "15"):
            with self.subTest(major=major), self.assertRaises(ValidationError):
                self.instance20.postgres_version = major
        self.instance20.postgres_version = "16"

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

    def test_a_series_on_odoo_db_gets_it(self):
        self.assertEqual(
            versions.script_env(self.instance20.odoo_version),
            {"IC_DB_TOOL": "odoo-db"},
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

    def test_the_importer_takes_only_an_offered_series(self):
        """A repository of a series not offered is imported with no
        version inferred, so the operator picks one the panel offers."""
        self.assertEqual(
            import_inspector._SUPPORTED_VERSIONS, set(versions.offered_versions()),
        )
        manifest = import_inspector.version_from_manifest_text
        self.assertEqual(manifest("{'version': '19.0.1.0.0'}"), "19.0")
        self.assertEqual(manifest("{'version': '20.0.1.0.0'}"), "20.0")
        self.assertEqual(manifest("{'version': '21.0.1.0.0'}"), "")
        answers = import_inspector._parse_copier_answers
        self.assertEqual(answers('odoo_version: "19.0"\n')["odoo_version"], "19.0")
        self.assertEqual(answers('odoo_version: "20.0"\n')["odoo_version"], "20.0")
        self.assertEqual(answers('odoo_version: "21.0"\n')["odoo_version"], "")


class TestANewRecordGetsTheNewestSeriesItsPanelOffers(TransactionCase):
    """What creates a project, or an instance outside a project, without
    naming a series gets the newest one offered that the panel's pin
    renders (owner's decision of 22-oct-2026). An instance in a project
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
            "20.0": versions._card(False, (16, 18, 17), "odoo-db", "v10.0.3"),
        }))
        self.manager = self._create_user("series_manager", "group_cloud_manager")
        self.assertFalse(self.manager.has_group("base.group_system"))

    def test_a_manager_cannot_create_a_project_of_it(self):
        Project = self.env["cloud.project"].with_user(self.manager)
        with self.assertRaises(ValidationError) as caught:
            Project.create({"name": "Too soon", "odoo_version": "20.0"})
        self.assertEqual(str(caught.exception), "Odoo 20.0 is not offered yet.")
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
        self.assertEqual(
            str(caught.exception), "Odoo 20.0 is not offered on this panel.",
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
