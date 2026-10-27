"""Check a tool's arguments against its input schema.

``http.route`` drops the keyword arguments a route does not declare, with
a warning nobody reads: a misspelt argument would be ignored in silence.
The tools' schemas are closed (``additionalProperties: false`` unless a
property says otherwise), so a key the tool does not know is an error the
agent sees. A deliberately small subset of JSON Schema: what the tools'
schemas use and nothing else.
"""

_PY_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


class SchemaError(ValueError):
    """Arguments that do not match a tool's input schema."""


def _check_number(schema, value, path):
    """Coerce *value* to the schema's number type and check its range."""
    kind = schema["type"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{path} must be an {kind}")
    if kind == "integer":
        if isinstance(value, float):
            if not value.is_integer():
                raise SchemaError(f"{path} must be an integer")
            value = int(value)
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaError(f"{path} must be at least {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaError(f"{path} must be at most {schema['maximum']}")
    return value


def _check_object(schema, value, path):
    """Check an object's keys and validate each declared property."""
    properties = schema.get("properties", {})
    unknown = sorted(set(value) - set(properties))
    if unknown and not schema.get("additionalProperties", False):
        raise SchemaError(f"{path}: unknown argument(s): {', '.join(unknown)}")
    missing = [name for name in schema.get("required", ()) if name not in value]
    if missing:
        raise SchemaError(f"{path}: missing argument(s): {', '.join(missing)}")
    return {
        key: validate(properties[key], item, f"{path}.{key}")
        if key in properties
        else item
        for key, item in value.items()
    }


def validate(schema, value, path="arguments"):
    """Return *value* checked against *schema*, with integers coerced.

    :param dict schema: the subset of JSON Schema the tools use: ``type``,
        ``properties``, ``required``, ``additionalProperties``, ``items``,
        ``enum``, ``minimum``, ``maximum``, ``maxLength``
    :param value: the decoded JSON value
    :param str path: where *value* sits, for the message
    :raises SchemaError: naming the first problem found
    """
    kind = schema.get("type")
    if kind in ("integer", "number"):
        value = _check_number(schema, value, path)
    elif kind in _PY_TYPES:
        if not isinstance(value, _PY_TYPES[kind]) or (
            kind != "boolean" and isinstance(value, bool)
        ):
            raise SchemaError(f"{path} must be of type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(
            f"{path} must be one of: {', '.join(map(str, schema['enum']))}"
        )
    if kind == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaError(f"{path} is longer than {schema['maxLength']} characters")
    if kind == "object":
        return _check_object(schema, value, path)
    if kind == "array" and "items" in schema:
        return [
            validate(schema["items"], item, f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value
