"""Command line for the script generator (SPEC-PATCH-005 P2).

    python cli.py generate --file orders.xlsx --tz Asia/Kolkata --instruction "sort by status ..."
    python cli.py generate --file orders.xlsx --tz Asia/Kolkata --config orders.config.json

Writes <name>.gs (paste into Extensions > Apps Script) and <name>.config.json (keep it: re-using
it regenerates the script without the instruction or a model call), and prints what the script
will do, anything the instruction mentioned that no rule uses, and what it would change in the
uploaded file today. Nothing is uploaded anywhere except the instruction and masked samples sent
to the model for compiling; nothing is kept.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.xlsx_reader import XlsxError
from app.agent.llm import LlmClient
from app.agent.onboarding import ModelPlan
from app.generate import Generated, generate

INSTALL = [
    "Open the spreadsheet in Google Sheets.",
    "Extensions > Apps Script. Delete what is in the editor and paste the .gs file.",
    "Save, choose installTrigger in the function list, and Run. Approve the permissions (it runs as you).",
    "Back in the sheet, use the new menu's Run now once to organise everything.",
]


def main(argv: Sequence[str] | None = None, *, llm_factory: Callable[[], tuple[LlmClient, ModelPlan]] | None = None,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="Generate an Apps Script from a sheet and an instruction.")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="upload an .xlsx and an instruction; get the script")
    g.add_argument("--file", required=True, type=Path, help="the .xlsx (download it from Sheets: File > Download)")
    g.add_argument("--tz", required=True, help="the spreadsheet's timezone, e.g. Asia/Kolkata (File > Settings)")
    how = g.add_mutually_exclusive_group(required=True)
    how.add_argument("--instruction", help="what to automate, in plain English")
    how.add_argument("--instruction-file", type=Path, help="the instruction, read from a text file")
    how.add_argument("--config", type=Path, help="a .config.json from an earlier run: no model call")
    g.add_argument("--out", type=Path, default=Path("."), help="where to write the .gs and .config.json")
    g.add_argument("--rows", type=int, default=0, help="also show up to N changed rows per tab, before and after")
    args = parser.parse_args(argv)

    if not args.file.is_file():
        return _error(f"no such file: {args.file}")
    upload = args.file.read_bytes()
    instruction = args.instruction
    if args.instruction_file is not None:
        instruction = args.instruction_file.read_text(encoding="utf-8")
    config_json = None
    if args.config is not None:
        try:
            config_json = json.loads(args.config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _error(f"cannot read {args.config}: {exc}")

    llm, plan = (None, None)
    if instruction is not None:
        try:
            llm, plan = (llm_factory or _openai)()
        except RuntimeError as exc:
            return _error(str(exc))
        _say(f"Compiling with {plan.primary} (then {plan.escalation} if needed)...")
    try:
        result = generate(upload, timezone=args.tz, instruction=instruction, config_json=config_json,
                          llm=llm, plan=plan, now=now or datetime.now(UTC))
    except XlsxError as exc:
        return _error(f"cannot read {args.file.name}: {exc}")

    if result.status != "OK" or result.script is None or result.config is None:
        return _report_failure(result)
    _print(result, args.tz, args.rows)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.file.stem
    script_path, config_path = args.out / f"{stem}.gs", args.out / f"{stem}.config.json"
    script_path.write_text(result.script, encoding="utf-8")
    config_path.write_text(json.dumps(result.config.model_dump(mode="json", by_alias=True, exclude_none=True),
                                      indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _say(f"\nWrote {script_path} and {config_path}.")
    _say("Install it:")
    for i, step in enumerate(INSTALL, 1):
        _say(f"  {i}. {step}")
    return 0


def _openai() -> tuple[LlmClient, ModelPlan]:
    from app.agent.llm import OpenAIClient
    from app.core.settings import get_settings

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("compiling an instruction needs OPENAI_API_KEY in backend/.env "
                           "(or reuse a .config.json with --config, which needs no model)")
    plan = ModelPlan(settings.llm_primary_model, settings.llm_escalation_model, settings.llm_primary_attempts,
                     settings.llm_escalation_attempts, settings.llm_max_turns_per_attempt, settings.llm_use_skills)
    return OpenAIClient(settings.openai_api_key), plan


# Typographic quotes and dashes (the readback uses them for the web page) read as garbage on a
# Windows console that is not UTF-8; the command line prints their plain equivalents.
_ASCII = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2014": "-",
                        "\u2013": "-", "\u2026": "..."})


def _say(*parts: object, file: Any = None) -> None:
    text = " ".join(str(p) for p in parts).translate(_ASCII)
    stream = file or sys.stdout
    stream.write(text.encode(stream.encoding or "utf-8", errors="replace").decode(stream.encoding or "utf-8") + "\n")


def _error(message: str) -> int:
    _say(f"error: {message}", file=sys.stderr)
    return 1


def _report_failure(result: Generated) -> int:
    headline = {"DECLINED": "No script: the instruction could not be turned into rules.",
                "REFUSED": "No script: the rules need something a generated script cannot do.",
                "INVALID_CONFIG": "No script: the config file cannot be used.",
                "MODEL_UNAVAILABLE": "No script: the model provider could not be used (nothing was compiled). "
                                     "Check the API key in backend/.env, or reuse a config with --config."
                }.get(result.status, "No script.")
    _say(headline)
    _say(f"  {result.reason}")
    _print_warnings(result)
    return 2


def _section(title: str) -> None:
    _say(f"\n{title}\n{'-' * len(title)}")


def _print(result: Generated, tz: str, rows: int) -> None:
    assert result.readback is not None and result.preview is not None
    _section("What the script will do")
    for line in result.readback.lines():
        _say(line)

    if result.notes:
        _section("Chosen for you")
        for note in result.notes:
            _say(f"  - {note}")

    if result.coverage:
        unmapped = result.unmapped
        _section("Check these" if unmapped else "Everything you mentioned is used")
        for m in unmapped:
            _say(f"  ! you mentioned {m.text!r} ({m.kind}), but {m.note}")
        covered = [m.text for m in result.coverage if m.covered and m.text != "(not said)"]
        if covered:
            _say(f"  used: {', '.join(covered)}")
        if any(m.text == "(not said)" for m in result.coverage):
            _say("  timing: you did not say when; see when each rule runs above")

    _print_warnings(result)
    targets = {getattr(r, "target_tab") for r in result.config.rules if r.action == "consolidate"} \
        if result.config else set()
    _print_preview(result.preview, tz, rows, targets)

    _section("What it does not do")
    for limit in result.limits:
        _say(f"  - {limit}")


def _print_warnings(result: Generated) -> None:
    if result.warnings:
        _section("About your file")
        for w in result.warnings:
            _say(f"  ! {w}")


def _print_preview(preview: dict[str, Any], tz: str, rows: int, rebuilt: set[str]) -> None:
    today = datetime.fromisoformat(preview["today"]).strftime("%d %b %Y")
    _section(f"If it ran now (as of {today}, {ZoneInfo(tz).key})")
    if preview["status"] == "PAUSED_DRIFT":
        _say("  Nothing would run: a governed tab's header row differs from when this config was made "
              f"({', '.join(d['tab'] for d in preview['drift'])}). Regenerate from the instruction.")
        return
    if preview["status"] != "OK":
        _say(f"  Nothing would be written: the run would stop ({preview['status'].lower()}).")
        for w in preview["warnings"][:5]:
            _say(f"  - {w}")
        return
    if not preview["tabs"]:
        _say("  Nothing to change: the sheet is already organised.")
    for t in preview["tabs"]:
        if not t["exists"]:
            summary = f"new tab with {t['rows_added']} rows"
        elif t["tab"] in rebuilt:  # rebuilt from its sources: what matters is how much differs
            n = t["changed_rows_total"]
            summary = f"rebuilt; {n} {'rows differ' if n != 1 else 'row differs'} from what is there now" if n \
                else "rebuilt; the same as what is there now"
        else:
            parts = [f"{n} {label}" for n, label in (
                (t["rows_to_reorder"], "rows reordered"), (t["rows_to_recolor"], "rows recoloured"),
                (t["rows_added"], "rows added"), (t["rows_removed"], "rows removed"),
                (t["rows_cleared"], "rows cleared")) if n]
            summary = ", ".join(parts) or "no change"
        _say(f"  {t['tab']}: {summary}")
        for s in t["sample"][:rows]:
            before = " | ".join("" if c["v"] is None else str(c["v"]) for c in s["before"])
            after = " | ".join("" if c["v"] is None else str(c["v"]) for c in s["after"])
            _say(f"    row {s['row']}: {before}\n       -> {after}")


if __name__ == "__main__":
    raise SystemExit(main())
