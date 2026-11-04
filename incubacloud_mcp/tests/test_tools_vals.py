"""The vals of the create and update tools: declared, checked, never dropped."""

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud_mcp.protocol.schema import RECORD_KEY

from .common import McpHttpMixin, McpUsersMixin, PanelFixtureMixin

#: The tools that take vals, and the model their fields are of.
VALS_TOOLS = {
    "create_project": "cloud.project",
    "update_project": "cloud.project",
    "create_instance": "cloud.instance",
    "update_instance": "cloud.instance",
}
#: Keys the routes read themselves, not fields of the model.
ROUTE_KEYS = {"repos", "domains", "domain", "whitelist"}
#: The JSON type each kind of field is declared with.
JSON_TYPES = {
    fields.Char: "string",
    fields.Text: "string",
    fields.Selection: "string",
    fields.Boolean: "boolean",
    fields.Integer: "integer",
    fields.Float: "number",
    fields.Many2one: "integer",
    fields.Many2many: "array",
}


@tagged("post_install", "-at_install")
class TestToolVals(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """What an agent sets is set, or refused with the reason; never dropped."""

    def setUp(self):
        """A consultant member of the project and a project manager."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "mcp-vals-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.consultant.id)]
        self.key = self._api_key(self.consultant)
        self.manager_key = self._api_key(
            self._cloud_user("mcp-vals-pm", group="group_cloud_project_manager")
        )
        self.spanish = (
            self.env["res.lang"]
            .with_context(active_test=False)
            .search([("code", "=", "es_ES")])
        )

    def _error(self, name, args, key=None):
        """Call *name*, check it failed, and return its message."""
        result = self._call_tool(key or self.key, name, args)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def test_an_unknown_field_is_refused_naming_the_accepted_ones(self):
        """A field the tool does not take is an error listing those it does,
        in the four tools; nothing is written."""
        self.staging.name = "kept"
        for name, args, key in (
            ("update_instance", {"instance_id": self.staging.id}, None),
            ("update_project", {"project_id": self.project.id}, None),
            ("create_instance", {}, None),
            ("create_project", {}, self.manager_key),
        ):
            with self.subTest(tool=name):
                vals = {"name": "changed", "odoo_lang": "es_ES"}
                text = self._error(name, {**args, "vals": vals}, key)
                self.assertIn("unknown argument(s): odoo_lang", text)
                self.assertIn("Accepted:", text)
                self.assertIn("odoo_initial_lang", text)
        self.assertEqual(self.staging.name, "kept")
        self.assertFalse(self.env["cloud.project"].search([("name", "=", "changed")]))

    def test_a_language_is_set_by_its_code(self):
        """es_ES sets the instance's and the project's initial language."""
        result = self._call_tool(
            self.key,
            "update_instance",
            {"instance_id": self.staging.id, "vals": {"odoo_initial_lang": "es_ES"}},
        )
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.staging.odoo_initial_lang, self.spanish)
        result = self._call_tool(
            self.key,
            "update_project",
            {"project_id": self.project.id, "vals": {"odoo_initial_lang": "es_ES"}},
        )
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.project.odoo_initial_lang, self.spanish)
        result = self._call_tool(
            self.key,
            "update_instance",
            {"instance_id": self.staging.id, "vals": {"odoo_initial_lang": None}},
        )
        self.assertFalse(result["isError"], result)
        self.assertFalse(self.staging.odoo_initial_lang)

    def test_a_language_that_does_not_exist_is_refused_with_the_codes(self):
        """A code no language has is refused, listing the codes there are."""
        for code in ("xx_XX", "es", "Spanish"):
            with self.subTest(code=code):
                text = self._error(
                    "update_instance",
                    {
                        "instance_id": self.staging.id,
                        "vals": {"odoo_initial_lang": code},
                    },
                )
                self.assertIn("arguments.vals.odoo_initial_lang", text)
                self.assertIn("es_ES", text)
                self.assertIn("en_US", text)
        self.assertFalse(self.staging.odoo_initial_lang)

    def test_an_id_field_given_text_is_refused(self):
        """Text, or an id as text, where a record's id goes is an error."""
        for name, args in (
            (
                "update_instance",
                {"instance_id": self.staging.id, "vals": {"host_id": "x"}},
            ),
            (
                "update_instance",
                {"instance_id": self.staging.id, "vals": {"backup_backend_id": "1"}},
            ),
            (
                "update_project",
                {"project_id": self.project.id, "vals": {"tag_ids": ["a"]}},
            ),
            ("create_instance", {"vals": {"project_id": "acme", "name": "x"}}),
            (
                "update_instance",
                {"instance_id": self.staging.id, "vals": {"odoo_initial_lang": True}},
            ),
            (
                "update_instance",
                {"instance_id": self.staging.id, "vals": {"odoo_version": True}},
            ),
        ):
            with self.subTest(args=args):
                text = self._error(name, args)
                self.assertIn("arguments.vals.", text)
                self.assertIn("must be", text)

    def test_the_declared_fields_are_the_routes_and_the_models(self):
        """Each declared field is one the route writes, of the model's type,
        with the model's choices; every field the routes take is declared,
        but the secrets; the description lists them all."""
        Catalog = self.env["incubacloud.mcp.catalog"]
        secrets = self.env["incubacloud.mcp.secrets"]._field_names()
        routes = {
            "create_instance": CloudDataLoadController._CREATE_INSTANCE_ALLOWED,
            "update_instance": CloudDataLoadController._SAVE_INSTANCE_ALLOWED,
            "create_project": CloudDataLoadController._PROJECT_ALLOWED,
            "update_project": CloudDataLoadController._PROJECT_ALLOWED,
        }
        #: What each route reads itself, besides the fields it writes.
        read = {
            "create_instance": {"repos", "domains", "domain"},
            "update_instance": {"repos", "domains", "whitelist", "environment"},
            "create_project": {"repos", "tag_ids"},
            "update_project": {"repos", "tag_ids", "member_ids"},
        }
        for name, model in VALS_TOOLS.items():
            spec = Catalog._spec(name)
            vals = spec.input_schema["properties"]["vals"]
            self.assertFalse(vals["additionalProperties"])
            Model = self.env[model]
            for field_name, schema in vals["properties"].items():
                with self.subTest(tool=name, field=field_name):
                    self.assertIn(f"{field_name} (", spec.description)
                    if field_name in ROUTE_KEYS:
                        continue
                    self.assertNotIn(field_name, secrets)
                    field = Model._fields[field_name]
                    expected = (
                        "string" if RECORD_KEY in schema else JSON_TYPES[type(field)]
                    )
                    kinds = schema["type"]
                    kinds = kinds if isinstance(kinds, list) else [kinds]
                    # The type a model writes first; null, or a number for
                    # a version, may follow.
                    self.assertEqual(kinds[0], expected)
                    if isinstance(field, fields.Selection) and "enum" in schema:
                        self.assertEqual(
                            {v for v in schema["enum"] if isinstance(v, str)},
                            {value for value, _label in field.selection},
                        )
                    if RECORD_KEY in schema:
                        self.assertEqual(
                            schema[RECORD_KEY]["model"], field.comodel_name
                        )
            declared = set(vals["properties"])
            self.assertEqual(routes[name] - secrets - declared, set(), name)
            # And every field declared is one the route takes: none is
            # dropped without a word.
            self.assertEqual(declared - routes[name] - read[name], set(), name)


@tagged("post_install", "-at_install")
class TestToolValsRoundTrip(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """What an agent reads it can write back: a language by its id, a version
    as a number, a backup destination when an instance is created."""

    def setUp(self):
        """A consultant and a developer of the project."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "mcp-trip-consultant", group="group_cloud_consultant"
        )
        self.developer = self._cloud_user(
            "mcp-trip-developer", group="group_cloud_developer"
        )
        self.project.member_ids = [(4, self.consultant.id), (4, self.developer.id)]
        self.key = self._api_key(self.consultant)
        self.developer_key = self._api_key(self.developer)
        self.spanish = (
            self.env["res.lang"]
            .with_context(active_test=False)
            .search([("code", "=", "es_ES")])
        )

    def _ok(self, name, args, key=None):
        """Call *name*; check it went through; return its answer."""
        result = self._call_tool(key or self.key, name, args)
        self.assertFalse(result["isError"], result["content"][-1]["text"][:500])
        return self._payload(result)

    def test_a_language_read_as_an_id_is_written_back_as_one(self):
        """get_instance and get_project answer the language's id: written
        back, it is the same language; an id no language has is refused."""
        self.staging.odoo_initial_lang = self.spanish
        self.project.odoo_initial_lang = self.spanish
        read = self._ok("get_instance", {"instance_id": self.staging.id})
        self.assertEqual(read["odoo_initial_lang"], self.spanish.id)
        self.staging.odoo_initial_lang = False
        self._ok(
            "update_instance",
            {"instance_id": self.staging.id,
             "vals": {"odoo_initial_lang": read["odoo_initial_lang"]}},
        )
        self.assertEqual(self.staging.odoo_initial_lang, self.spanish)
        project = self._ok("get_project", {"project_id": self.project.id})
        self.project.odoo_initial_lang = False
        self._ok(
            "update_project",
            {"project_id": self.project.id,
             "vals": {"odoo_initial_lang": project["odoo_initial_lang"]}},
        )
        self.assertEqual(self.project.odoo_initial_lang, self.spanish)
        missing = self.env["res.lang"].with_context(active_test=False).search(
            [], order="id desc", limit=1,
        ).id + 1000
        result = self._call_tool(
            self.key, "update_instance",
            {"instance_id": self.staging.id, "vals": {"odoo_initial_lang": missing}},
        )
        self.assertTrue(result["isError"])
        self.assertIn(f"no res.lang has the id {missing}", result["content"][0]["text"])

    def test_a_version_given_as_a_number_is_the_series(self):
        """19 and 19.0 are the series 19.0, as the panel's form sends them;
        a number no series has is refused."""
        series = self.staging.odoo_version
        project_series = self.project.odoo_version
        for number in (float(series), int(float(series))):
            with self.subTest(number=number):
                self._ok(
                    "update_instance",
                    {"instance_id": self.staging.id, "vals": {"odoo_version": number}},
                )
                self.assertEqual(self.staging.odoo_version, series)
                self._ok(
                    "update_project",
                    {"project_id": self.project.id,
                     "vals": {"odoo_version": float(project_series)}},
                )
                self.assertEqual(self.project.odoo_version, project_series)
        result = self._call_tool(
            self.key, "update_instance",
            {"instance_id": self.staging.id, "vals": {"odoo_version": 3.5}},
        )
        self.assertTrue(result["isError"])
        self.assertIn("must be one of", result["content"][0]["text"])

    def test_a_new_instance_keeps_the_backup_destination_it_is_given(self):
        """create_instance writes backup_backend_id, under the rule the
        panel's form is under: a developer chooses it; a consultant is
        told why not, and nothing is created."""
        vals = {
            "name": "mcp-trip-prod",
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": "staging",
            "backup_backend_id": self.backend.id,
        }
        result = self._call_tool(self.key, "create_instance", {"vals": vals})
        self.assertTrue(result["isError"])
        self.assertIn("Developer", result["content"][-1]["text"])
        self.assertFalse(
            self.env["cloud.instance"].search([("name", "=", "mcp-trip-prod")])
        )
        self._ok("create_instance", {"vals": vals}, key=self.developer_key)
        created = self.env["cloud.instance"].search([("name", "=", "mcp-trip-prod")])
        self.assertEqual(created.backup_backend_id, self.backend)

    def test_the_answers_never_cut_what_an_update_writes_back_whole(self):
        """The guard knows the fields the update tools replace whole."""
        whole = self.env["incubacloud.mcp.server"]._replaced_whole()
        for key in (
            "repos", "domains", "whitelist", "member_ids", "tag_ids",
            "pip_dependencies", "apt_dependencies", "odoo_conf",
        ):
            with self.subTest(key=key):
                self.assertIn(key, whole)
        self.assertNotIn("recentJobs", whole)
