"""The product never talks to Google (PATCH-005 A, amendment I.1).

The sheets adapter, the authorised HTTP client and the service-account credentials survive only
so the live proof (P4 / PATCH-004 D.3) can read a test spreadsheet back. They live in
`tests/live/`, and nothing under `app/` may import them: that import fence is what keeps
"test-only" true once this file is six weeks old and nobody remembers the rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

FORBIDDEN_PREFIXES = ("tests", "googleapiclient", "google.oauth2", "google_auth_httplib2", "httplib2",
                      "google.auth")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
    return out


def test_no_product_module_imports_the_google_path_or_the_tests() -> None:
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for module in imported_modules(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.relative_to(BACKEND)} imports {module}")
    assert offenders == [], "the product must not reach Google or the test-only package:\n" + "\n".join(offenders)


def test_the_fenced_package_is_where_the_google_client_lives() -> None:
    live = BACKEND / "tests" / "live"
    assert (live / "sheets_adapter.py").exists() and (live / "google_http.py").exists()
    assert not list(APP.rglob("sheets_adapter.py")) and not list(APP.rglob("google_http.py"))


def test_settings_never_default_to_a_live_credential() -> None:
    """A missing key must fail loudly rather than quietly picking up an ambient account."""
    from app.core.settings import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not getattr(settings, "google_service_account_json", "")
    assert not getattr(settings, "google_service_account_file", "")
