"""PATCH-003 A.3: cell values are on screen, never in storage. The frontend must not log or persist
anything -- previews and sample rows carry cell values -- so no console.* and no browser storage
anywhere in frontend/src (tests excluded: they may stub these)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
FORBIDDEN = {
    "console output": re.compile(r"\bconsole\s*\.\s*\w+"),
    "localStorage": re.compile(r"\blocalStorage\b"),
    "sessionStorage": re.compile(r"\bsessionStorage\b"),
    "IndexedDB": re.compile(r"\bindexedDB\b"),
    "cookies": re.compile(r"\bdocument\s*\.\s*cookie\b"),
    "analytics beacons": re.compile(r"\bnavigator\s*\.\s*sendBeacon\b"),
}


def source_files() -> list[Path]:
    if not SRC.exists():
        pytest.skip("frontend not present")
    return [p for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx", ".js", ".jsx"}
            and not re.search(r"\.test\.(ts|tsx|js|jsx)$", p.name)]


def test_frontend_never_logs_or_persists_data() -> None:
    files = source_files()
    assert files, "no frontend sources found"
    hits = [f"{p.relative_to(SRC)}:{i}: {label}"
            for p in files
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            for label, rx in FORBIDDEN.items() if rx.search(line)]
    assert not hits, "\n".join(hits)


def test_guard_catches_a_violation(tmp_path: Path) -> None:
    sample = "const rows = preview.tabs; console.log(rows); localStorage.setItem('p', JSON.stringify(rows));"
    found = {label for label, rx in FORBIDDEN.items() if rx.search(sample)}
    assert found == {"console output", "localStorage"}
