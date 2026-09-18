"""Export ConfigSpec as JSON Schema to docs/config.schema.json.

Usage (from /backend):  uv run python -m scripts.export_config_schema
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.schemas.config import SCHEMA_VERSION, ConfigSpec

OUT = Path(__file__).resolve().parents[2] / "docs" / "config.schema.json"


def build_schema() -> dict[str, Any]:
    schema = ConfigSpec.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://sheets-automation.local/schemas/config.v{SCHEMA_VERSION}.json"
    schema["x-schema-version"] = SCHEMA_VERSION
    return schema


def render() -> str:
    return json.dumps(build_schema(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
