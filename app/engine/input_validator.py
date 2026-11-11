"""Validates an incoming payload against the workflow's configured input schema.

Returns *all* errors (not just the first) so clients can fix everything in one go.
"""
from __future__ import annotations

import re
from typing import Any

from app.configuration.models import FieldSpec

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_input(payload: Any, schema: dict[str, FieldSpec]) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return [{"field": "$", "error": "request body must be a JSON object"}]

    errors: list[dict[str, str]] = []
    for name, spec in schema.items():
        if name not in payload or payload[name] is None:
            if spec.required:
                errors.append({"field": name, "error": "is required"})
            continue
        value = payload[name]
        if not _TYPE_CHECKS[spec.type](value):
            errors.append({"field": name, "error": f"must be of type {spec.type}"})
            continue
        if spec.enum is not None and value not in spec.enum:
            errors.append({"field": name, "error": f"must be one of {spec.enum}"})
        if spec.type in ("integer", "number"):
            if spec.min is not None and value < spec.min:
                errors.append({"field": name, "error": f"must be >= {spec.min:g}"})
            if spec.max is not None and value > spec.max:
                errors.append({"field": name, "error": f"must be <= {spec.max:g}"})
        if spec.type in ("string", "array"):
            if spec.min_length is not None and len(value) < spec.min_length:
                errors.append({"field": name, "error": f"length must be >= {spec.min_length}"})
            if spec.max_length is not None and len(value) > spec.max_length:
                errors.append({"field": name, "error": f"length must be <= {spec.max_length}"})
        if spec.type == "string" and spec.pattern and not re.fullmatch(spec.pattern, value):
            errors.append({"field": name, "error": f"must match pattern {spec.pattern}"})
    return errors
