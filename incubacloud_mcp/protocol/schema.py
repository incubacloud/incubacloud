"""Check a tool's arguments against its input schema.

``http.route`` drops the keyword arguments a route does not declare, with
a warning nobody reads: a misspelt argument would be ignored in silence.
The tools' schemas are closed (``additionalProperties: false`` unless a
property says otherwise), so a key the tool does not know is an error the
agent sees, and so is a value of the wrong type. A deliberately small
subset of JSON Schema: what the tools' schemas use and nothing else, plus
``nullable`` (a property that also takes null, as OpenAPI says it) and
:data:`RECORD_KEY`, a property that names a record by one of its fields.
"""

#: The key of a property that names a record by one of its fields:
#: ``{"model": "res.lang", "field": "code"}``. :func:`resolve` turns the
#: value into the record's id; the listing does not show the key.
RECORD_KEY = "x-record"
#: How many accepted values a refusal lists at most.
_MAX_LISTED = 200

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
        accepted = ", ".join(sorted(properties)) or "none"
        raise SchemaError(
            f"{path}: unknown argument(s): {', '.join(unknown)}. Accepted: {accepted}."
        )
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
        ``enum``, ``minimum``, ``maximum``, ``maxLength``, ``nullable``
    :param value: the decoded JSON value
    :param str path: where *value* sits, for the message
    :raises SchemaError: naming the first problem found
    """
    if value is None and schema.get("nullable"):
        return None
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


def resolve(schema, value, lookup, path="arguments"):
    """Return validated *value* with every record named by a field as its id.

    Walks *value* along *schema* as :func:`validate` does; a property
    marked with :data:`RECORD_KEY` is looked up, null left as it is.

    :param dict schema: the schema *value* was validated against
    :param value: the validated value
    :param lookup: ``lookup(model, field, text)`` → the id of the record
        whose *field* is *text*, or None; ``lookup(model, field, None)``
        → every value of *field*, for the refusal
    :param str path: where *value* sits, for the message
    :raises SchemaError: when no record has that value, naming the ones
        accepted
    """
    record = schema.get(RECORD_KEY)
    if record and value is not None:
        found = lookup(record["model"], record["field"], value)
        if found is None:
            known = lookup(record["model"], record["field"], None)
            listed = ", ".join(known[:_MAX_LISTED])
            more = ", …" if len(known) > _MAX_LISTED else ""
            raise SchemaError(f"{path}: {value!r} is not one of: {listed}{more}.")
        return found
    if isinstance(value, dict) and schema.get("type") == "object":
        properties = schema.get("properties", {})
        return {
            key: resolve(properties[key], item, lookup, f"{path}.{key}")
            if key in properties
            else item
            for key, item in value.items()
        }
    if isinstance(value, list) and "items" in schema:
        return [
            resolve(schema["items"], item, lookup, f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value
