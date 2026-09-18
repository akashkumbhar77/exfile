"""B.7 masking boundary: nothing the onboarding agent's sample_rows tool returns contains an
original free-text value; structure (headers, tab names, status labels) passes through."""

from __future__ import annotations

import json
import random
import re
from datetime import date

import pytest

from app.agent.masking import Masker, is_enum_column
from app.agent.profile import build_profile
from app.agent.tools import ToolError, sample_rows
from app.services.grid import Tab, Workbook, cell_text
from tests.fixtures import reference_workbook

CONTACTS = Tab("CONTACTS", [
    ["CUSTOMER CONTACTS"],
    ["CUSTOMER NAME", "CONTACT PERSON", "EMAIL", "PHONE", "ORDER NO", "AMOUNT", "RATE", "ORDER DATE", "STATUS", "PAID?"],
    ["Acme Industries Pvt Ltd", "Ravi Sharma", "ravi.sharma@acme.co.in", "+91 98765 43210", "WO-2024-0117",
     125000, 12.5, date(2026, 3, 1), "Completed", "YES"],
    ["Beta Engineering", "Anita Desai", "anita@beta-eng.com", "022-2654-7788", "WO-2024-0118",
     98000, 7.25, date(2026, 4, 15), "In Process", "NO"],
    ["Acme Industries Pvt Ltd", "Ravi Sharma", "ravi.sharma@acme.co.in", "+91 98765 43210", "WO-2024-0121",
     4500, 3.0, date(2026, 2, 10), "Completed", "YES"],
    ["Gamma Tools", "Suresh Iyer", "s.iyer@gammatools.in", "9820012345", "INV1",
     310000, 18.75, date(2026, 5, 20), "Disputed", "NO"],
])


def workbook() -> Workbook:
    wb = reference_workbook.build()
    wb.tabs.append(CONTACTS.clone())
    return wb


def free_text_values(tab: Tab, header_row: int = 2) -> set[str]:
    headers = tab.row(header_row)
    out: set[str] = set()
    for j, h in enumerate(headers):
        col = [r[j] for r in tab.values[header_row:] if j < len(r)]
        if is_enum_column(h, col):
            continue
        out |= {cell_text(v).strip() for v in col if isinstance(v, str) and len(v.strip()) >= 2}
    return out


@pytest.mark.parametrize("tab_name", ["CONTACTS", "MACHINES", "SPARES"])
@pytest.mark.parametrize("seed", range(5))
def test_no_original_free_text_in_serialized_tool_result(tab_name: str, seed: int) -> None:
    wb = workbook()
    payload = sample_rows(wb, tab_name, n=50, rng=random.Random(seed))
    tab = wb.tab(tab_name)
    assert tab is not None
    originals = free_text_values(tab)
    assert originals, "fixture must contain free text"
    # every masked cell differs from its own original (n=50 samples every data row, in order)
    body = json.loads(payload)
    data = [r for r in tab.values[2:] if any(cell_text(v).strip() for v in r)]
    assert len(body["rows"]) == len(data)
    enum_idx = {j for j, h in enumerate(tab.row(2)) if is_enum_column(h, [r[j] for r in data if j < len(r)])}
    for orig, masked in zip(data, body["rows"]):
        for j, v in enumerate(orig):
            if j not in enum_idx and isinstance(v, str) and v.strip():
                assert masked[j] != v, f"column {j} kept its original value"
    # ... and no original value of 4+ characters appears anywhere (shorter codes can coincide by chance)
    leaked = {v for v in originals if len(v) >= 4 and v.lower() in payload.lower()}
    assert not leaked, f"leaked: {sorted(leaked)}"
    # word-level too: no multi-letter token of a name/company survives (e.g. 'Sharma', 'Acme')
    tokens = {t.lower() for v in originals for t in re.findall(r"[A-Za-z]{4,}", v)}
    structure = " ".join(cell_text(h) for h in tab.row(2)).lower()
    leaked_tokens = {t for t in tokens if t in payload.lower() and t not in structure}
    assert not leaked_tokens, f"leaked tokens: {sorted(leaked_tokens)}"


def test_structure_passes_through_unmasked() -> None:
    body = json.loads(sample_rows(workbook(), "CONTACTS", n=4, rng=random.Random(1)))
    assert body["tab"] == "CONTACTS"
    assert body["headers"][:4] == ["CUSTOMER NAME", "CONTACT PERSON", "EMAIL", "PHONE"]
    statuses = {r[8] for r in body["rows"]}
    paid = {r[9] for r in body["rows"]}
    assert statuses == {"Completed", "In Process", "Disputed"} and paid == {"YES", "NO"}


def test_placeholders_stable_within_a_call_and_typed_by_column() -> None:
    body = json.loads(sample_rows(workbook(), "CONTACTS", n=4, rng=random.Random(2)))
    rows = body["rows"]
    customers = [r[0] for r in rows]
    assert all(re.fullmatch(r"Company_[A-Z]+", c) for c in customers)
    assert len(set(customers)) == 3  # Acme twice -> same placeholder; Beta, Gamma distinct
    assert all(re.fullmatch(r"Person_[A-Z]+", r[1]) for r in rows)
    by_orig = {}
    for orig, masked in zip([r[0] for r in CONTACTS.values[2:]], customers):
        assert by_orig.setdefault(orig, masked) == masked


def test_placeholders_differ_across_calls() -> None:
    a = json.loads(sample_rows(workbook(), "CONTACTS", n=4, rng=random.Random(3)))["rows"]
    b = json.loads(sample_rows(workbook(), "CONTACTS", n=4, rng=random.Random(4)))["rows"]
    assert [r[4] for r in a] != [r[4] for r in b]  # random codes
    assert [r[7] for r in a] != [r[7] for r in b]  # different date offsets


def test_shapes_magnitudes_and_date_order_preserved() -> None:
    m = Masker(random.Random(5))
    rows = m.mask_rows(CONTACTS.row(2), [r for r in CONTACTS.values[2:]], enum_cols={8, 9})
    for orig, masked in zip(CONTACTS.values[2:], rows):
        email, phone, code = masked[2], masked[3], masked[4]
        assert re.fullmatch(r"[^@]+@[^@]+\.example", str(email))
        assert len(str(email).split("@")[0]) == len(str(orig[2]).split("@")[0])
        assert re.sub(r"\d", "9", str(phone)) == re.sub(r"\d", "9", str(orig[3]))  # same format
        assert re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", str(code))) == \
            re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", str(orig[4])))
        assert isinstance(masked[5], int) and len(str(masked[5])) == len(str(orig[5]))
        assert isinstance(masked[6], float) and 0.5 * float(orig[6]) <= masked[6] <= 1.5 * float(orig[6])  # type: ignore[arg-type]
    shifts = {(mr[7] - o[7]).days for o, mr in zip(CONTACTS.values[2:], rows)}  # type: ignore[operator]
    assert len(shifts) == 1 and shifts != {0}  # one constant offset per call
    orig_order = sorted(range(4), key=lambda i: CONTACTS.values[2 + i][7])  # type: ignore[arg-type, return-value]
    new_order = sorted(range(4), key=lambda i: rows[i][7])  # type: ignore[arg-type, return-value]
    assert orig_order == new_order


def test_low_cardinality_names_are_not_mistaken_for_enum_labels() -> None:
    assert not is_enum_column("CUSTOMER NAME", ["Acme", "Acme", "Beta", "Beta"])
    assert is_enum_column("STATUS", ["anything"])
    assert is_enum_column("WORK ORDER FREEZE?", ["YES", "NO"])
    assert is_enum_column("DONE", ["Y", "N"])


def test_profile_holds_structure_and_distributions_only() -> None:
    wb = workbook()
    text = json.dumps(build_profile(wb, "Asia/Kolkata"))
    for tab in wb.tabs:
        if tab.height < 3:
            continue
        leaked = {v for v in free_text_values(tab) if v in text}
        assert not leaked, f"profile leaks {sorted(leaked)} from {tab.name}"
    prof = build_profile(wb, "Asia/Kolkata")
    contacts = next(t for t in prof["tabs"] if t["name"] == "CONTACTS")
    status = next(c for c in contacts["columns"] if c["header"] == "STATUS")
    assert status["enum"] == {"Completed": 2, "In Process": 1, "Disputed": 1}
    assert contacts["header_row"] == 2 and contacts["data_rows"] == 4


def test_sample_size_bounds() -> None:
    with pytest.raises(ToolError):
        sample_rows(workbook(), "CONTACTS", n=51)
    with pytest.raises(ToolError):
        sample_rows(workbook(), "NOPE", n=5)


def test_show_masked_sample(capsys: pytest.CaptureFixture[str]) -> None:
    """Not an assertion-heavy test: prints one masked payload for human review (-s)."""
    payload = json.loads(sample_rows(workbook(), "CONTACTS", n=4, rng=random.Random(7)))
    print("\nMASKED sample_rows(CONTACTS, n=4):")
    print("  headers:", payload["headers"])
    for row in payload["rows"]:
        print("  ", row)
