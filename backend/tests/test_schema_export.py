from __future__ import annotations

from scripts.export_config_schema import OUT, build_schema, render


def test_committed_schema_is_current() -> None:
    assert OUT.exists(), "run: uv run python -m scripts.export_config_schema"
    assert OUT.read_text(encoding="utf-8") == render(), "docs/config.schema.json is stale; re-export it"


def test_schema_covers_all_actions() -> None:
    text = render()
    for action in ("sort", "format", "consolidate", "move", "copy", "validate", "dedupe", "clear"):
        assert f'"const": "{action}"' in text
    assert build_schema()["additionalProperties"] is False
