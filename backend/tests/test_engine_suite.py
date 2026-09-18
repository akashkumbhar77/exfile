"""Runs the Engine.gs Node suite from pytest and keeps the shared fixtures in sync."""

from __future__ import annotations

import subprocess

import pytest

from scripts.export_engine_fixtures import OUT_DIR, render
from tests.test_engine_python_parity import NODE, ROOT


def test_engine_fixtures_are_current() -> None:
    for name, text in render().items():
        path = OUT_DIR / name
        assert path.exists() and path.read_text(encoding="utf-8") == text, (
            f"{path} is stale: run `uv run python -m scripts.export_engine_fixtures`"
        )


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_engine_node_suite_passes() -> None:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "--test", "--test-reporter=tap", str(ROOT / "engine" / "test" / "engine.test.js")],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    summary = [line for line in proc.stdout.splitlines() if line.startswith("# ")]
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "# fail 0" in summary, summary
