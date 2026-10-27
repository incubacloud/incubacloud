"""An argument the route would silently drop is refused instead."""
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.schema import SchemaError, validate

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

    def test_valid_arguments_come_back_with_integers_coerced(self):
        self.assertEqual(
            validate(SCHEMA, {"instance_id": 3.0, "ids": [1, 2], "vals": {"x": 1}, "anything": [1]}),
            {"instance_id": 3, "ids": [1, 2], "vals": {"x": 1}, "anything": [1]},
        )

    def test_an_unknown_argument_is_named(self):
        with self.assertRaisesRegex(SchemaError, "postgres_pasword"):
            validate(SCHEMA, {"instance_id": 1, "postgres_pasword": "x"})

    def test_a_missing_required_argument_is_named(self):
        with self.assertRaisesRegex(SchemaError, "instance_id"):
            validate(SCHEMA, {})

    def test_types_are_checked(self):
        for bad in ({"instance_id": "7"}, {"instance_id": True}, {"instance_id": 1, "force": 1},
                    {"instance_id": 1, "ids": [1, "2"]}, {"instance_id": 1, "vals": []}):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(SCHEMA, bad)

    def test_enum_range_and_length(self):
        for bad in ({"instance_id": 1, "environment": "dev"}, {"instance_id": 0},
                    {"instance_id": 1, "name": "toolong"}):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate(SCHEMA, bad)

    def test_an_open_object_accepts_any_key(self):
        self.assertEqual(validate(SCHEMA, {"instance_id": 1, "vals": {"whatever": 1}})["vals"],
                         {"whatever": 1})
