"""The subset of JSON Schema harnesses/proof-audit/schema.json uses, checked without a dependency: type (one or a
list), enum, items, properties, required and additionalProperties: false."""


KINDS = {"object": dict, "array": list, "string": str, "null": type(None)}


def kind_matches(value, kind):
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, KINDS[kind])


def schema_problem(value, schema, where="grades"):
    """The first way `value` breaks the subset of JSON Schema schema.json uses, or None."""
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds or []
    if kinds and not any(kind_matches(value, kind) for kind in kinds):
        return "%s is not %s" % (where, " or ".join(kinds))
    if "enum" in schema and value not in schema["enum"]:
        return "%s is %r, not one of %s" % (where, value, ", ".join(schema["enum"]))
    if isinstance(value, dict):
        return object_problem(value, schema, where)
    if isinstance(value, list):
        problems = (schema_problem(item, schema.get("items", {}), "%s[%d]" % (where, index)) for index, item in enumerate(value))
        return next((problem for problem in problems if problem), None)
    return None


def object_problem(value, schema, where):
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        return "%s lacks %s" % (where, ", ".join(missing))
    extra = [key for key in value if key not in properties] if schema.get("additionalProperties") is False else []
    if extra:
        return "%s has %s" % (where, ", ".join(extra))
    problems = (schema_problem(value[key], sub, "%s.%s" % (where, key)) for key, sub in properties.items() if key in value)
    return next((problem for problem in problems if problem), None)
