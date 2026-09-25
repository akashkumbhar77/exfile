"""P2: upload + instruction (or an earlier config.json) -> readback, coverage, preview and the script."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


import pytest

from app.agent.llm import LlmReply
from app.agent.onboarding import ModelPlan
from app.generate import generate
from app.schemas.config import ConfigSpec
from cli import main
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook
from tests.test_onboarding import ScriptedLlm, call, compiled_reference
from tests.xlsx_builder import Cell, Sheet, build, from_workbook

TZ = "Asia/Kolkata"
NOW = datetime(2026, 9, 16, 4, 30, tzinfo=UTC)   # 10:00 in Kolkata
PLAN = ModelPlan("fake-mini", "fake-large", primary_attempts=1, escalation_attempts=1, use_skills=False)
INSTRUCTION = ("Sort MACHINES and SPARES by status stage then dispatch date, colour overdue rows red, "
               "and rebuild a SUMMARY tab from them")
UPLOAD = from_workbook(reference_workbook.build())


def scripted(*replies: LlmReply) -> ScriptedLlm:
    return ScriptedLlm({"fake-mini": list(replies)})


def test_an_instruction_becomes_a_script_with_its_readback_and_preview() -> None:
    llm = scripted(call("propose_config", {"config": compiled_reference()}))
    result = generate(UPLOAD, timezone=TZ, instruction=INSTRUCTION, llm=llm, plan=PLAN, now=NOW)
    assert result.status == "OK", result.reason
    assert result.script is not None and "function installTrigger()" in result.script
    assert result.readback is not None and len(result.readback.rules) == 3
    assert result.preview is not None and result.preview["today"] == "2026-09-16"
    assert result.preview["timezone"] == TZ
    assert result.unmapped == [], [m.text for m in result.unmapped]
    assert any("_backup" in limit or "No undo" in limit for limit in result.limits)


def test_a_config_from_an_earlier_download_needs_no_model() -> None:
    result = generate(UPLOAD, timezone=TZ, config_json=load_reference_raw(), now=NOW)
    assert result.status == "OK" and result.script is not None
    assert result.coverage == [], "there is no instruction to check against"


def test_mentions_the_rules_do_not_use_are_listed() -> None:
    llm = scripted(call("propose_config", {"config": compiled_reference()}))
    result = generate(UPLOAD, timezone=TZ, llm=llm, plan=PLAN, now=NOW,
                      instruction=INSTRUCTION + ", and highlight QTY in purple every morning")
    unmapped = {(m.kind, m.text) for m in result.unmapped}
    assert unmapped == {("column", "QTY"), ("colour", "purple"), ("timing", "every morning")}


def test_a_request_the_script_cannot_meet_is_declined_in_plain_words() -> None:
    """The model first proposes a 5-minute schedule; the compile says the script cannot, and the
    model declines. No script, and the reason is the model's plain sentence."""
    too_often = compiled_reference()
    too_often["rules"][0]["trigger"] = {"schedule": {"cron": "*/5 * * * *"}}
    decline = LlmReply(content="CANNOT: a generated script can run at most once an hour, not every 5 minutes.",
                       tool_calls=(), input_tokens=10)
    # a rejected proposal ends the attempt; the next one (the escalation model) sees why and declines
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": too_often})], "fake-large": [decline]})
    result = generate(UPLOAD, timezone=TZ, instruction="sort by status every 5 minutes", llm=llm, plan=PLAN, now=NOW)
    assert result.status == "DECLINED" and result.script is None
    assert "at most once an hour" in result.reason
    rejection = next(m["content"] for _, sent in llm.sent for m in sent if m["role"] == "tool")
    assert json.loads(rejection)["stage"] == "script"


def test_formulas_in_rows_a_sort_moves_are_warned_about_by_column() -> None:
    workbook = reference_workbook.build()
    sheets = []
    for tab in workbook.tabs:
        cells = {(r, c): Cell(v) for r, row in enumerate(tab.values, 1) for c, v in enumerate(row, 1)
                 if v not in (None, "")}
        if tab.name == "MACHINES":
            for r in range(3, tab.height + 1):
                cells[(r, 4)] = Cell(tab.values[r - 1][3], formula=f"B{r}*0+1")
        sheets.append(Sheet(tab.name, cells))
    result = generate(build(sheets), timezone=TZ, config_json=load_reference_raw(), now=NOW)
    assert result.status == "OK"
    formula = [w for w in result.warnings if "formulas" in w]
    assert formula == [formula[0]] and formula[0].startswith("MACHINES: 10 data cell(s) in column(s) D hold")


def test_a_config_whose_headers_changed_previews_as_paused() -> None:
    workbook = reference_workbook.build()
    machines = workbook.tab("MACHINES")
    assert machines is not None
    machines.values[1][1] = "CLIENT"
    result = generate(from_workbook(workbook), timezone=TZ, config_json=load_reference_raw(), now=NOW)
    assert result.status == "OK" and result.preview is not None
    assert result.preview["status"] == "PAUSED_DRIFT"


def test_a_broken_config_file_is_explained() -> None:
    raw = load_reference_raw()
    raw["rules"][0]["action"] = "shuffle"
    assert generate(UPLOAD, timezone=TZ, config_json=raw, now=NOW).status == "INVALID_CONFIG"
    raw = load_reference_raw()
    raw["schema_hashes"]["ARCHIVE"] = "x"
    result = generate(UPLOAD, timezone=TZ, config_json=raw, now=NOW)
    assert result.status == "INVALID_CONFIG" and "ARCHIVE" in result.reason


# ---------------------------------------------------------------- the command line

def run_cli(tmp_path: Path, *args: str, llm: ScriptedLlm | None = None) -> int:
    upload = tmp_path / "orders.xlsx"
    upload.write_bytes(UPLOAD)
    return main(["generate", "--file", str(upload), "--tz", TZ, "--out", str(tmp_path / "out"), *args],
                llm_factory=(lambda: (llm, PLAN)) if llm else None, now=NOW)


def test_the_cli_writes_the_script_and_config_and_prints_the_review(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    llm = scripted(call("propose_config", {"config": compiled_reference()}))
    assert run_cli(tmp_path, "--instruction", INSTRUCTION, "--rows", "1", llm=llm) == 0
    out = capsys.readouterr().out
    for heading in ("What the script will do", "Everything you mentioned is used",
                    "If it ran now (as of 16 Sep 2026, Asia/Kolkata)", "What it does not do", "Install it:"):
        assert heading in out, heading
    assert "MACHINES: 9 rows reordered" in out and "SUMMARY: new tab with" in out
    script = (tmp_path / "out" / "orders.gs").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "out" / "orders.config.json").read_text(encoding="utf-8"))
    assert "function organizeNow()" in script
    assert ConfigSpec.model_validate(saved).rules[0].id == "sort_by_stage"


def test_the_saved_config_regenerates_the_same_script(tmp_path: Path) -> None:
    llm = scripted(call("propose_config", {"config": compiled_reference()}))
    assert run_cli(tmp_path, "--instruction", INSTRUCTION, llm=llm) == 0
    first = (tmp_path / "out" / "orders.gs").read_text(encoding="utf-8")
    saved = tmp_path / "saved.config.json"
    saved.write_text((tmp_path / "out" / "orders.config.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert run_cli(tmp_path, "--config", str(saved)) == 0
    assert (tmp_path / "out" / "orders.gs").read_text(encoding="utf-8") == first


def test_the_cli_explains_a_decline_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    decline = LlmReply(content="CANNOT: there is no column that says which rows are urgent.", tool_calls=(),
                       input_tokens=10)
    assert run_cli(tmp_path, "--instruction", "put urgent rows first", llm=scripted(decline)) == 2
    out = capsys.readouterr().out
    assert "No script" in out and "which rows are urgent" in out
    assert not (tmp_path / "out").exists()


def test_the_cli_reports_an_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "orders.xlsx"
    bad.write_bytes(b"not a spreadsheet")
    code = main(["generate", "--file", str(bad), "--tz", TZ, "--config", str(bad)], now=NOW)
    assert code == 1


def test_the_cli_needs_a_timezone(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["generate", "--file", "x.xlsx", "--instruction", "sort"])
    assert "--tz" in capsys.readouterr().err


