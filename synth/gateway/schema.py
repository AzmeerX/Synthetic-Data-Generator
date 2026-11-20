"""JSON-schema helpers: strict schemas from Pydantic models, empty valid instances."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A self-contained strict schema: refs inlined, every property required,
    ``additionalProperties: false``, titles and defaults dropped.

    This is the shape ``response_format: json_schema, strict: true`` expects
    (LM Studio / llama.cpp grammars and OpenAI alike).
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[name]))
            out = {}
            for key, value in node.items():
                if key in ("title", "default", "examples"):
                    continue
                out[key] = resolve(value)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"].keys())
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(raw)


def empty_instance(schema: dict[str, Any]) -> Any:
    """The smallest value that validates against ``schema`` (ScriptedModelClient default)."""
    if "anyOf" in schema:
        options = schema["anyOf"]
        if any(o.get("type") == "null" for o in options):
            return None
        return empty_instance(options[0])
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0]
    if kind == "object":
        props = schema.get("properties", {})
        return {k: empty_instance(v) for k, v in props.items() if k in schema.get("required", props)}
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return None
