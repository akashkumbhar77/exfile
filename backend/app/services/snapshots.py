"""Before-snapshots (invariant 6): gzip JSON of a tab's full state, restorable exactly.

Stored as bytea in Postgres from M3; this module only (de)serializes.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.services.grid import CellFormat, CellValue, DataValidation, Tab, Workbook, a1_range


@dataclass(frozen=True, repr=False)
class Snapshot:
    run_id: str
    rule_id: str
    tab: str
    range_a1: str
    existed: bool  # False = the rule created the tab; undo removes it
    body_gz: bytes

    def __repr__(self) -> str:  # B.6: gzip bodies contain literal cell text
        return (f"Snapshot(run_id={self.run_id!r}, rule_id={self.rule_id!r}, tab={self.tab!r}, "
                f"range_a1={self.range_a1!r}, existed={self.existed}, body=<redacted {len(self.body_gz)}B>)")


def _enc_cell(v: CellValue) -> Any:
    if isinstance(v, datetime):
        return {"$datetime": v.isoformat()}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    return v


def _dec_cell(v: Any) -> CellValue:
    if isinstance(v, dict):
        if "$datetime" in v:
            return datetime.fromisoformat(v["$datetime"])
        if "$date" in v:
            return date.fromisoformat(v["$date"])
        raise ValueError(f"bad cell encoding (type {type(v).__name__})")
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    raise ValueError(f"bad cell encoding (type {type(v).__name__})")


def encode_tab(tab: Tab) -> bytes:
    body = {
        "name": tab.name,
        "values": [[_enc_cell(v) for v in row] for row in tab.values],
        "formats": [[[f.font, f.background, f.strike] for f in row] for row in tab.formats],
        "validations": [
            [r, c, list(dv.values), dv.allow_invalid] for (r, c), dv in sorted(tab.validations.items())
        ],
        "protected": tab.protected,
    }
    return gzip.compress(json.dumps(body, separators=(",", ":")).encode("utf-8"), mtime=0)


def decode_tab(blob: bytes) -> Tab:
    body = json.loads(gzip.decompress(blob))
    return Tab(
        name=body["name"],
        values=[[_dec_cell(v) for v in row] for row in body["values"]],
        formats=[[CellFormat(f[0], f[1], f[2]) for f in row] for row in body["formats"]],
        validations={(r, c): DataValidation(tuple(vals), allow) for r, c, vals, allow in body["validations"]},
        protected=body["protected"],
    )


def take_snapshot(run_id: str, rule_id: str, tab_name: str, before: Tab | None) -> Snapshot:
    if before is None:
        return Snapshot(run_id, rule_id, tab_name, "A1:A1", False, encode_tab(Tab(tab_name, [])))
    return Snapshot(
        run_id, rule_id, tab_name, a1_range(before.height, before.width), True, encode_tab(before)
    )


def restore_snapshots(workbook: Workbook, snapshots: list[Snapshot]) -> Workbook:
    """Undo: restore snapshots newest-first so each tab returns to its earliest captured state."""
    out = workbook.clone()
    for snap in reversed(snapshots):
        if not snap.existed:
            out.tabs = [t for t in out.tabs if t.name != snap.tab]
        else:
            out.put(decode_tab(snap.body_gz))
    return out
