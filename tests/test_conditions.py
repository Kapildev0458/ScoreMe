"""Unit tests for the condition DSL (pure functions, no I/O)."""
import pytest

from app.engine.conditions import describe_check, evaluate, register_operator, validate_condition

CTX = {"age": 30, "income": 50000, "credit": {"score": 720}, "tags": ["a", "b"], "flag": False, "none": None}


@pytest.mark.parametrize("cond,expected", [
    ({"field": "age", "op": "gte", "value": 21}, True),
    ({"field": "age", "op": "lt", "value": 21}, False),
    ({"field": "age", "op": "between", "value": [21, 60]}, True),
    ({"field": "credit.score", "op": "gt", "value": 700}, True),      # nested path
    ({"field": "tags", "op": "contains", "value": "b"}, True),
    ({"field": "age", "op": "in", "value": [29, 30]}, True),
    ({"field": "age", "op": "not_in", "value": [29, 30]}, False),
    ({"field": "flag", "op": "is_false"}, True),
    ({"field": "missing", "op": "exists"}, False),
    ({"field": "none", "op": "not_exists"}, True),
    ({"field": "income", "op": "gte", "ref": "age", "factor": 1000}, True),   # 50000 >= 30*1000
])
def test_operators(cond, expected):
    assert evaluate(cond, CTX)[0] is expected


def test_combinators_and_full_trace():
    cond = {"all": [{"field": "age", "op": "gte", "value": 18},
                    {"any": [{"field": "credit.score", "op": "gte", "value": 800},
                             {"field": "income", "op": "gt", "value": 10000}]},
                    {"not": {"field": "flag", "op": "is_true"}}]}
    passed, trace = evaluate(cond, CTX)
    assert passed is True
    # No short-circuit: every leaf is recorded for explainability.
    assert len(trace) == 4
    assert trace[-1]["negated"] is True


def test_missing_field_fails_safely_with_note():
    passed, trace = evaluate({"field": "credit.limit", "op": "gt", "value": 1}, CTX)
    assert passed is False and trace[0]["note"] == "field missing"
    assert "<missing>" in describe_check(trace[0])


def test_type_mismatch_does_not_crash():
    passed, trace = evaluate({"field": "tags", "op": "gt", "value": 5}, CTX)
    assert passed is False and "evaluation error" in trace[0]["note"]


def test_static_validation_catches_bad_conditions():
    assert validate_condition({"field": "x", "op": "nope", "value": 1})
    assert validate_condition({"field": "x", "op": "gt"})                    # missing value
    assert validate_condition({"field": "x", "op": "between", "value": 5})   # bad between
    assert validate_condition({"all": []})
    assert validate_condition({"field": "x", "op": "regex", "value": "("})   # invalid regex
    assert validate_condition({"field": "x", "op": "gt", "value": 1}) == []


def test_custom_operator_extension_point():
    register_operator("divisible_by", lambda a, b: a % b == 0, "divisible by")
    assert evaluate({"field": "age", "op": "divisible_by", "value": 5}, CTX)[0] is True
    assert validate_condition({"field": "age", "op": "divisible_by", "value": 5}) == []
