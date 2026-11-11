"""Condition DSL evaluator.

A condition is plain data (YAML/JSON), never code, so rules can be changed by
editing configuration and can be safely accepted over an API (no ``eval``).

Grammar::

    condition := leaf | {"all": [condition, ...]} | {"any": [condition, ...]} | {"not": condition}
    leaf      := {"field": "<dot.path>", "op": "<operator>",
                  "value": <literal>?            # compare against a constant, or
                  "ref": "<dot.path>"?, "factor": <number>?}   # against another field (* factor)

Every evaluation returns a trace of each leaf check (field, actual value,
operator, expected value, pass/fail) which is what makes decisions explainable.
"""
from __future__ import annotations

import re
from typing import Any, Callable

MISSING = object()

# --------------------------------------------------------------------------- operators
Operator = Callable[[Any, Any], bool]

OPERATORS: dict[str, Operator] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "between": lambda a, b: b[0] <= a <= b[1],
    "contains": lambda a, b: b in a,
    "not_contains": lambda a, b: b not in a,
    "regex": lambda a, b: re.fullmatch(b, str(a)) is not None,
    "is_true": lambda a, _b: a is True,
    "is_false": lambda a, _b: a is False,
}
# Operators that take no expected value.
UNARY_OPERATORS = {"exists", "not_exists", "is_true", "is_false"}
KNOWN_OPERATORS = set(OPERATORS) | {"exists", "not_exists"}

SYMBOLS = {
    "eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "in": "in", "not_in": "not in", "between": "between", "contains": "contains",
    "not_contains": "does not contain", "regex": "matches", "exists": "exists",
    "not_exists": "does not exist", "is_true": "is true", "is_false": "is false",
}


def register_operator(name: str, fn: Operator, symbol: str | None = None) -> None:
    """Extension point: add a new operator without touching the evaluator."""
    OPERATORS[name] = fn
    KNOWN_OPERATORS.add(name)
    SYMBOLS[name] = symbol or name


# --------------------------------------------------------------------------- helpers
def resolve_path(data: Any, path: str) -> Any:
    """Resolve ``a.b.c`` inside nested dicts; returns ``MISSING`` if absent."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _display(value: Any) -> Any:
    return "<missing>" if value is MISSING else value


# --------------------------------------------------------------------------- evaluation
def evaluate(condition: dict[str, Any], context: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate a condition. Returns (passed, leaf_trace)."""
    trace: list[dict[str, Any]] = []
    passed = _eval(condition, context, trace)
    return passed, trace


def _eval(cond: dict[str, Any], ctx: dict[str, Any], trace: list[dict[str, Any]]) -> bool:
    if "all" in cond:
        # Evaluate every child (no short-circuit) so the trace is complete.
        results = [_eval(c, ctx, trace) for c in cond["all"]]
        return all(results)
    if "any" in cond:
        results = [_eval(c, ctx, trace) for c in cond["any"]]
        return any(results)
    if "not" in cond:
        sub: list[dict[str, Any]] = []
        result = not _eval(cond["not"], ctx, sub)
        for entry in sub:
            entry["negated"] = True
        trace.extend(sub)
        return result
    return _eval_leaf(cond, ctx, trace)


def _eval_leaf(cond: dict[str, Any], ctx: dict[str, Any], trace: list[dict[str, Any]]) -> bool:
    field, op = cond["field"], cond["op"]
    actual = resolve_path(ctx, field)
    entry: dict[str, Any] = {"field": field, "op": op, "actual": _display(actual)}

    if op == "exists":
        result = actual is not MISSING and actual is not None
    elif op == "not_exists":
        result = actual is MISSING or actual is None
    else:
        expected = _expected(cond, ctx)
        if op not in UNARY_OPERATORS:
            entry["expected"] = _display(expected)
        if "ref" in cond:
            entry["expected_from"] = f"{cond['ref']}" + (f" * {cond['factor']}" if "factor" in cond else "")
        if actual is MISSING or actual is None:
            result, entry["note"] = False, "field missing"
        elif expected is MISSING:
            result, entry["note"] = False, f"reference field '{cond.get('ref')}' missing"
        else:
            try:
                result = bool(OPERATORS[op](actual, expected))
            except (TypeError, ValueError, IndexError, re.error) as exc:
                result, entry["note"] = False, f"evaluation error: {exc}"

    entry["passed"] = result
    trace.append(entry)
    return result


def _expected(cond: dict[str, Any], ctx: dict[str, Any]) -> Any:
    if "ref" in cond:
        ref_value = resolve_path(ctx, cond["ref"])
        if ref_value is MISSING or ref_value is None:
            return MISSING
        factor = cond.get("factor")
        return ref_value * factor if factor is not None else ref_value
    return cond.get("value")


# --------------------------------------------------------------------------- static validation
def validate_condition(cond: Any, path: str = "condition") -> list[str]:
    """Validate condition structure at config-load time (fail fast, not at runtime)."""
    if not isinstance(cond, dict):
        return [f"{path}: must be an object"]
    combinators = [k for k in ("all", "any", "not") if k in cond]
    if len(combinators) > 1:
        return [f"{path}: only one of all/any/not allowed per node"]
    if combinators:
        key = combinators[0]
        if len(cond) != 1:
            return [f"{path}: '{key}' node must not have other keys"]
        if key == "not":
            return validate_condition(cond["not"], f"{path}.not")
        children = cond[key]
        if not isinstance(children, list) or not children:
            return [f"{path}.{key}: must be a non-empty list"]
        errors: list[str] = []
        for i, child in enumerate(children):
            errors.extend(validate_condition(child, f"{path}.{key}[{i}]"))
        return errors

    errors = []
    allowed_keys = {"field", "op", "value", "ref", "factor"}
    unknown = set(cond) - allowed_keys
    if unknown:
        errors.append(f"{path}: unknown keys {sorted(unknown)}")
    if not isinstance(cond.get("field"), str) or not cond.get("field"):
        errors.append(f"{path}: 'field' is required")
    op = cond.get("op")
    if op not in KNOWN_OPERATORS:
        errors.append(f"{path}: unknown operator '{op}' (known: {sorted(KNOWN_OPERATORS)})")
    elif op not in UNARY_OPERATORS and "value" not in cond and "ref" not in cond:
        errors.append(f"{path}: operator '{op}' needs 'value' or 'ref'")
    if "value" in cond and "ref" in cond:
        errors.append(f"{path}: use either 'value' or 'ref', not both")
    if "factor" in cond and "ref" not in cond:
        errors.append(f"{path}: 'factor' only allowed with 'ref'")
    if op == "between" and not (isinstance(cond.get("value"), list) and len(cond["value"]) == 2):
        errors.append(f"{path}: 'between' needs value [low, high]")
    if op in ("in", "not_in") and "value" in cond and not isinstance(cond["value"], list):
        errors.append(f"{path}: '{op}' needs a list value")
    if op == "regex" and "value" in cond:
        try:
            re.compile(cond["value"])
        except re.error as exc:
            errors.append(f"{path}: invalid regex: {exc}")
    return errors


# --------------------------------------------------------------------------- explanation
def describe_check(entry: dict[str, Any]) -> str:
    """Human-readable form of one leaf check, e.g. ``credit.score=580 (required >= 650)``."""
    symbol = SYMBOLS.get(entry["op"], entry["op"])
    prefix = "NOT " if entry.get("negated") else ""
    if entry["op"] in UNARY_OPERATORS:
        text = f"{prefix}{entry['field']}={entry['actual']!r} (required: {symbol})"
    else:
        expected = entry.get("expected")
        source = f" [= {entry['expected_from']}]" if "expected_from" in entry else ""
        text = f"{prefix}{entry['field']}={entry['actual']!r} (required {symbol} {expected!r}{source})"
    if entry.get("note"):
        text += f" - {entry['note']}"
    return text
