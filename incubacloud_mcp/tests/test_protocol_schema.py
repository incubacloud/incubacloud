"""An argument the route would silently drop is refused instead."""

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.schema import (
    RECORD_KEY,
    SchemaError,
    resolve,
    validate,
)
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    describe_fields,
    nullable,
    obj,
    record_id,
    record_key,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "instance_id": {"type": "integer", "minimum": 1},
        "name": {"type": "string", "maxLength": 5},
        "force": {"type": "boolean"},
        "ids": {"type": "array", "items": {"type": "integer"}},
        "environment": {"type": "string", "enum": ["staging", "production"]},
        "vals": {"type": "object", "additionalProperties": True},
        "anything": {"description": "No type: any JSON value."},
    },
    "required": ["instance_id"],
    "additionalProperties": False,
}


class TestSchema(BaseCase):
    """Arguments are checked against the tool's schema, unknown keys included."""

    def test_valid_arguments_come_back_with_integers_coerced(self):
        """Valid arguments come back, integral floats as integers."""
        self.assertEqual(
            validate(
                SCHEMA,
                {"instance_id": 3.0, "ids": [1, 2], "vals": {"x": 1}, "anything": [1]},
            ),
            {"instance_id": 3, "ids": [1, 2], "vals": {"x": 1}, "anything": [1]},
        )

    def test_an_unknown_argument_is_named(self):
        """An argument the tool does not know is refused by name."""
        with self.assertRaisesRegex(SchemaError, "postgres_pasword"):
            validate(SCHEMA, {"instance_id": 1, "postgres_pasword": "x"})

    def test_a_missing_required_argument_is_named(self):
        """A missing required argument is refused by name."""
        with self.assertRaisesRegex(SchemaError, "instance_id"):
            validate(SCHEMA, {})

    def test_types_are_checked(self):
        """Every JSON type is checked; a boolean is not an integer."""
        for bad in (
            {"instance_id": "7"},
            {"instance_id": True},
            {"instance_id": 1, "force": 1},
            {"instance_id": 1, "ids": [1, "2"]},
            {"instance_id": 1, "vals": []},
        ):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(SCHEMA, bad)

    def test_enum_range_and_length(self):
        """enum, minimum, maximum and maxLength are enforced."""
        for bad in (
            {"instance_id": 1, "environment": "dev"},
            {"instance_id": 0},
            {"instance_id": 1, "name": "toolong"},
        ):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(SCHEMA, bad)

    def test_an_open_object_accepts_any_key(self):
        """An object open to more properties takes any key."""
        self.assertEqual(
            validate(SCHEMA, {"instance_id": 1, "vals": {"whatever": 1}})["vals"],
            {"whatever": 1},
        )


#: The languages the lookup of these tests knows, by code.
LANGS = {"en_US": 1, "es_ES": 7}


def _lookup(model, field, value):
    """Answer as the server's lookup does, from :data:`LANGS`."""
    assert (model, field) == ("res.lang", "code")
    return sorted(LANGS) if value is None else LANGS.get(value)


VALS = obj(
    {
        "vals": obj(
            {
                "host_id": record_id("A server."),
                "backup_backend_id": nullable(record_id("A destination.")),
                "lang": nullable(record_key("A language.", "res.lang", "code")),
                "langs": {
                    "type": "array",
                    "items": record_key("L.", "res.lang", "code"),
                },
            }
        )
    }
)


class TestFieldsByKey(BaseCase):
    """Closed fields name the accepted ones; a record is given by its key."""

    def test_an_unknown_field_names_the_accepted_ones(self):
        """The refusal lists every key the object takes."""
        with self.assertRaises(SchemaError) as caught:
            validate(VALS, {"vals": {"odoo_lang": "es_ES"}})
        self.assertEqual(
            str(caught.exception),
            "arguments.vals: unknown argument(s): odoo_lang. "
            "Accepted: backup_backend_id, host_id, lang, langs.",
        )

    def test_an_id_field_refuses_text_and_takes_null_only_when_nullable(self):
        """Text where an id goes is refused; null only where the field allows."""
        for bad in (
            {"host_id": "srv-1"},
            {"host_id": None},
            {"backup_backend_id": "1"},
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(SchemaError, "integer"):
                validate(VALS, {"vals": bad})
        self.assertEqual(
            validate(VALS, {"vals": {"backup_backend_id": None}}),
            {"vals": {"backup_backend_id": None}},
        )

    def test_a_record_by_its_code_becomes_its_id(self):
        """A language's code turns into its id, in lists too; null stays."""
        args = validate(
            VALS, {"vals": {"lang": "es_ES", "langs": ["en_US"], "host_id": 3}}
        )
        self.assertEqual(
            resolve(VALS, args, _lookup),
            {"vals": {"lang": 7, "langs": [1], "host_id": 3}},
        )
        self.assertEqual(
            resolve(VALS, {"vals": {"lang": None}}, _lookup), {"vals": {"lang": None}}
        )

    def test_a_code_no_record_has_is_refused_with_the_accepted_ones(self):
        """An unknown code is refused, listing the codes there are."""
        for vals, path in (
            ({"lang": "xx_XX"}, "arguments.vals.lang"),
            ({"langs": ["es_ES", "es"]}, "arguments.vals.langs[1]"),
        ):
            with self.subTest(vals=vals), self.assertRaises(SchemaError) as caught:
                resolve(VALS, validate(VALS, {"vals": vals}), _lookup)
            self.assertIn(path, str(caught.exception))
            self.assertIn("en_US, es_ES", str(caught.exception))

    def test_the_listing_and_the_description_show_the_fields_not_the_key(self):
        """The listed schema has no server key; the fields' types are named."""
        spec = ToolSpec("t", "T.", "group_cloud_user", VALS, route="/x")
        listed = str(spec.listed()["inputSchema"])
        self.assertNotIn(RECORD_KEY, listed)
        # JSON Schema's way: null is one of the types, not OpenAPI's key.
        self.assertIn("'null'", listed)
        self.assertNotIn("nullable", listed)
        self.assertIn(RECORD_KEY, str(spec.input_schema))
        self.assertEqual(
            describe_fields(VALS["properties"]["vals"]),
            "host_id (id); backup_backend_id (id or null); "
            "lang (string, a code of res.lang or null); "
            "langs (array of string, a code of res.lang)",
        )


class TestTypeLists(BaseCase):
    """A property of several types takes a value of any of them, and says so."""

    def test_null_is_one_of_the_types(self):
        """``nullable`` lists null among the types; a boolean is no integer."""
        schema = nullable(record_id("A destination."))
        self.assertEqual(schema["type"], ["integer", "null"])
        self.assertIsNone(validate(schema, None))
        self.assertEqual(validate(schema, 3.0), 3)
        for bad in (True, "3", 0):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(schema, bad)

    def test_a_record_is_named_by_its_code_or_by_its_id(self):
        """With or_id, the id the readers answer is taken, checked to be a
        record's; a code still becomes its id."""
        schema = nullable(record_key("A language.", "res.lang", "code", or_id=True))
        self.assertEqual(schema["type"], ["string", "integer", "null"])

        def lookup(model, field, value):
            """Languages 1 and 7, by code and by id."""
            if field == "id":
                return value if value in (1, 7) else None
            return _lookup(model, field, value)

        self.assertEqual(resolve(schema, validate(schema, 7), lookup), 7)
        self.assertEqual(resolve(schema, validate(schema, "es_ES"), lookup), 7)
        self.assertIsNone(resolve(schema, validate(schema, None), lookup))
        with self.assertRaisesRegex(SchemaError, "no res.lang has the id 9"):
            resolve(schema, validate(schema, 9), lookup)
        with self.assertRaisesRegex(SchemaError, "string or integer or null"):
            validate(schema, True)
        self.assertEqual(
            describe_fields(obj({"lang": schema})),
            "lang (string, a code of res.lang, or its id or null)",
        )

    def test_a_series_is_text_or_a_number(self):
        """The version's enum holds the series as text and as numbers."""
        schema = {
            "type": ["string", "number"],
            "enum": ["18.0", "19.0", 18.0, 19.0],
            "description": "The series.",
        }
        for value in ("19.0", 19, 19.0):
            with self.subTest(value=value):
                self.assertEqual(validate(schema, value), value)
        for bad in ("19", 20, False):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(schema, bad)
        self.assertEqual(
            describe_fields(obj({"odoo_version": schema})),
            "odoo_version (18.0|19.0 (or as a number))",
        )
