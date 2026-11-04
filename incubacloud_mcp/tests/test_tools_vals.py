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
                {"instance_id": self.staging.id, "vals": {"odoo_initial_lang": 12}},
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
                    self.assertEqual(schema["type"], expected)
                    if isinstance(field, fields.Selection) and "enum" in schema:
                        self.assertEqual(
                            set(schema["enum"]),
                            {value for value, _label in field.selection},
                        )
                    if RECORD_KEY in schema:
                        self.assertEqual(
                            schema[RECORD_KEY]["model"], field.comodel_name
                        )
            if name in routes:
                self.assertEqual(
                    routes[name] - secrets - set(vals["properties"]), set(), name
                )
