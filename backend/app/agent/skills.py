"""Agent skills: reference docs the onboarding model loads on demand via the `load_skill` tool.

Each `skills/<name>.md` has front matter (name, description, schema_defs) and a body. The core
prompt carries only the catalogue (name + one-line description); a loaded skill returns its
body plus the JSON Schema definitions it lists, so the model gets the exact contract for the
parts of the config it is using instead of the whole schema on every call.

Examples inside skills use a neutral domain (support tickets), never a customer workbook.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.schemas.config import ConfigSpec

SKILLS_DIR = Path(__file__).parent / "skills"
_FRONT = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    schema_defs: tuple[str, ...]
    body: str


def _parse(path: Path) -> Skill:
    m = _FRONT.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
    if not m:
        raise ValueError(f"{path.name}: missing front matter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    defs = tuple(d.strip() for d in meta.get("schema_defs", "").strip("[]").split(",") if d.strip())
    return Skill(meta["name"], meta["description"], defs, m.group(2).strip())


@lru_cache
def catalogue() -> dict[str, Skill]:
    skills = {s.name: s for s in (_parse(p) for p in sorted(SKILLS_DIR.glob("*.md")))}
    known = set(ConfigSpec.model_json_schema(by_alias=True)["$defs"])
    for s in skills.values():
        missing = set(s.schema_defs) - known
        if missing:
            raise ValueError(f"skill {s.name}: unknown schema defs {sorted(missing)}")
    return skills


def catalogue_text() -> str:
    return "\n".join(f"- `{s.name}`: {s.description}" for s in catalogue().values())


def render(names: list[str]) -> str:
    """Bodies of the requested skills plus their JSON Schema definitions (deduplicated)."""
    skills = catalogue()
    unknown = [n for n in names if n not in skills]
    if unknown:
        raise KeyError(f"unknown skill(s) {unknown}; available: {sorted(skills)}")
    defs_all: dict[str, Any] = ConfigSpec.model_json_schema(by_alias=True)["$defs"]
    parts: list[str] = []
    wanted: list[str] = []
    for n in dict.fromkeys(names):
        parts.append(skills[n].body)
        wanted += [d for d in skills[n].schema_defs if d not in wanted]
    schema = {d: defs_all[d] for d in wanted}
    parts.append("## JSON Schema definitions for these skills\n```json\n"
                 + json.dumps(schema, separators=(",", ":")) + "\n```")
    return "\n\n".join(parts)
