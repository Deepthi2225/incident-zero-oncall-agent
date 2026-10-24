"""A small JSON Schema checker for our own schemas (no dependency). Supports the subset
schemas/incident.schema.json uses: type (incl. lists), enum, required, properties,
additionalProperties, items, minimum, anyOf and local $ref (#/$defs/...)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCIDENT_SCHEMA = json.loads((ROOT / "schemas" / "incident.schema.json").read_text(encoding="utf-8"))

_TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def errors(instance, schema=None, root=None, path="$"):
    """List of problems; empty means valid."""
    schema = INCIDENT_SCHEMA if schema is None else schema
    root = root or schema
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        return errors(instance, node, root, path)
    if "anyOf" in schema:
        branches = [errors(instance, s, root, path) for s in schema["anyOf"]]
        if any(not b for b in branches):
            return []
        # report the closest branch whose type matched, so nested problems keep their real paths
        typed = [b for b in branches if not (len(b) == 1 and b[0].startswith(f"{path}: expected"))]
        return min(typed or branches, key=len)
    out = []
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        if not any(_TYPES[t](instance) for t in types):
            return [f"{path}: expected {'/'.join(types)}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        out.append(f"{path}: {instance!r} not in {schema['enum']}")
    if "minimum" in schema and _TYPES["number"](instance) and instance < schema["minimum"]:
        out.append(f"{path}: {instance} < {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                out.append(f"{path}: missing {key!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                out += errors(value, props[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: unexpected {key!r}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            out += errors(item, schema["items"], root, f"{path}[{i}]")
    return out
