"""Validator: the reference config passes; every invalid-corpus entry is rejected with structured errors."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.services.validator import validate_config
from tests.conftest import FIXTURES, load_reference_raw

CORPUS: list[dict[str, Any]] = json.loads((FIXTURES / "invalid_configs.json").read_text(encoding="utf-8"))


def _resolve(doc: Any, pointer: str) -> tuple[Any, str]:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    node = doc
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, list) else node[p]
    return node, parts[-1]


def apply_patch(doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    for op in ops:
        parent, key = _resolve(out, op["path"])
        if op["op"] == "set":
            if isinstance(parent, list):
                parent[int(key)] = op["value"]
            else:
                parent[key] = op["value"]
        elif op["op"] == "remove":
            del parent[int(key) if isinstance(parent, list) else key]
        elif op["op"] == "append":
            (parent[int(key)] if isinstance(parent, list) else parent[key]).append(op["value"])
        else:
            raise ValueError(op["op"])
    return out


def test_reference_config_is_valid() -> None:
    result = validate_config(load_reference_raw())
    assert result.ok, result.errors
    assert result.config is not None


def test_reference_config_valid_from_json_text() -> None:
    assert validate_config((FIXTURES / "reference_config.json").read_text(encoding="utf-8")).ok


def test_corpus_is_substantial() -> None:
    assert len(CORPUS) >= 50
    assert len({c["name"] for c in CORPUS}) == len(CORPUS)


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_invalid_config_rejected(case: dict[str, Any]) -> None:
    result = validate_config(apply_patch(load_reference_raw(), case["patch"]))
    assert not result.ok
    assert result.config is None
    assert result.errors, "rejection must carry structured errors"
    for err in result.errors:
        assert err.pointer == "" or err.pointer.startswith("/")
        assert err.code and err.message
    got = [(e.code, e.pointer) for e in result.errors]
    for exp in case["expect"]:
        matches = [g for g in got if g[1] == exp["pointer"] and ("code" not in exp or g[0] == exp["code"])]
        assert matches, f"expected {exp} in {got}"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", "42"])
def test_non_object_input_rejected(raw: str) -> None:
    result = validate_config(raw)
    assert not result.ok
    assert result.errors[0].pointer == ""


def test_errors_serialize_for_agent() -> None:
    bad = apply_patch(load_reference_raw(), CORPUS[0]["patch"])
    payload = validate_config(bad).model_dump()
    assert set(payload) == {"ok", "errors"}
    assert set(payload["errors"][0]) == {"pointer", "code", "message"}


def test_multiple_errors_reported_together() -> None:
    raw = apply_patch(load_reference_raw(), [
        {"op": "set", "path": "/rules/0/keys/1/column", "value": "SHIP DATE"},
        {"op": "set", "path": "/rules/1/row_rules/0/when/is", "value": "ON-HOLD"},
    ])
    codes = {e.code for e in validate_config(raw).errors}
    assert codes == {"unknown_column", "unknown_enum_value"}


def test_column_references_are_case_insensitive() -> None:
    raw = apply_patch(load_reference_raw(), [{"op": "set", "path": "/rules/0/keys/1/column", "value": "dispatch date"}])
    assert validate_config(raw).ok
