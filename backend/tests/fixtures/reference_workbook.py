"""Reference workbook shaped like the sheet legacy.gs was written for.

Row 1 = title banner, row 2 = headers, data from row 3. Header variants
("TENTATIVE DISPATCH DATE" vs "DISPATCH DATE", "WORK ORDER FREEZE?" vs
"TECHNICAL FREEZE?") exercise canonicalization. TODAY is fixed.
"""

from __future__ import annotations

from datetime import date

from app.services.grid import Tab, Workbook

TODAY = date(2026, 9, 16)

MACHINES_HEADERS = [
    "SR NO", "CUSTOMER NAME", "MACHINE NAME", "QTY",
    "TENTATIVE DISPATCH DATE", "STATUS", "WORK ORDER FREEZE?", "PANEL IN-HOUSE OR OUTSOURCE?",
]
SPARES_HEADERS = [
    "SR NO", "CUSTOMER NAME", "Type of Work", "DISPATCH DATE", "STATUS", "TECHNICAL FREEZE?", "INVOICE NO",
]

MACHINES_ROWS = [
    [1, "Acme", "Lathe", 2, date(2026, 10, 1), "Completed", "YES", "In-House"],
    [2, "Beta", "Mill", 1, date(2026, 9, 1), "A. IN PROCESS", "NO", "Outsource"],  # overdue
    [3, "Gamma", "Press", 1, "", "In-Process", "", ""],  # blank date
    [4, "Delta", "Drill", 3, date(2026, 9, 20), "IN-PROCESS", "YES", ""],  # future, frozen
    [5, "Eps", "Saw", 1, date(2026, 8, 1), "Cancelled", "", ""],
    [6, "Zeta", "Grinder", 1, date(2026, 7, 1), "ON HOLD", "", ""],  # unknown stage
    [7, "Eta", "Router", 1, date(2026, 6, 1), "Dispatched", "yes", ""],
    [8, "Theta", "CNC", 2, date(2026, 9, 10), "Disputed", "", ""],
    [9, "Iota", "Bender", 1, date(2026, 9, 5), "", "", ""],  # blank status
    [10, "Kappa", "Welder", 1, date(2026, 9, 15), "in process", "", ""],  # overdue (yesterday)
]

SPARES_ROWS = [
    ["S1", "Omega", "", date(2026, 9, 30), "Completed", "", "INV1"],  # Type of Work blank -> "SPARES"
    ["S2", "Sigma", "Repair", date(2026, 9, 2), "In Process", "YES", ""],  # overdue
    ["", "", "", "", "", "", ""],  # blank row in the middle
    ["S3", "Tau", "", "", "Disputed", "", ""],
]

# Expected SR NO order on MACHINES after the stage sort (see test_sort).
MACHINES_SORTED_SR = [2, 10, 4, 3, 1, 8, 7, 5, 6, 9]


def _tab(name: str, title: str, headers: list[str], rows: list[list[object]]) -> Tab:
    return Tab(name, [[title], list(headers), *[list(r) for r in rows]])  # type: ignore[list-item]


def build() -> Workbook:
    return Workbook([
        Tab("LEGENDS", [["LEGEND"], ["COLOUR", "MEANING"], ["Red", "In process"]]),
        _tab("MACHINES", "MACHINE ORDERS", MACHINES_HEADERS, MACHINES_ROWS),
        _tab("SPARES", "SPARES ORDERS", SPARES_HEADERS, SPARES_ROWS),
    ])
