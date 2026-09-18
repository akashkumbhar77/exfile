"""Guard on tracked .env* files (added after a real API token was committed into
backend/.env.example on 2026-09-18, see DECISIONS "Pre-merge hardening").

Two independent layers:
  1. backend/.env.example is pinned: its keys and values must equal EXPECTED exactly, so any change
     to the template is a deliberate edit of this test.
  2. every tracked .env* file (working tree AND the staged index version) passes a secret check:
     secret-named keys must be empty, and no value may contain a long high-entropy token.
A real `.env` must never be tracked.
"""

from __future__ import annotations

import math
import re
import secrets
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = "backend/.env.example"

EXPECTED: dict[str, str] = {
    "GOOGLE_APPLICATION_CREDENTIALS": r"C:\path\to\service-account.json",
    "TEST_SHEET_ID": "",
    "ENROLLED_SHEET_IDS": "",
    "SNAPSHOT_KEY": "",
    "SNAPSHOT_KEY_ID": "k1",
    "SNAPSHOT_DIR": "var/snapshots",
    "SNAPSHOT_RETENTION_DAYS": "30",
    "DATABASE_URL": "postgresql+psycopg://sheets:sheets@localhost:5432/sheets",  # docker-compose dev default
    "REDIS_URL": "redis://localhost:6379/0",
    "ORG_ID": "org_default",
    "POLL_INTERVAL_SECONDS": "30",
    "DEBOUNCE_SECONDS": "30",
    "OPENAI_API_KEY": "",
    "LLM_PRIMARY_MODEL": "gpt-5-mini",
    "LLM_ESCALATION_MODEL": "gpt-5.1",
    "LLM_USE_SKILLS": "true",
    "API_TOKEN": "",
    "CORS_ORIGINS": "http://localhost:5173",
}

SECRET_NAME = re.compile(r"(^|_)(API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY)$", re.I)
SEPARATORS = re.compile(r"[\s:/@.+?=&,;\\\"'()]+")  # not '-' or '_': url-safe tokens stay whole
MIN_TOKEN = 20
MAX_ENTROPY = 3.5  # bits per char; random base64/url-safe secrets are ~5+


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip().removeprefix("export ").strip()] = value
    return out


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def read_env_bytes(raw: bytes) -> str | None:
    """UTF-8 text, or None when the file isn't plain UTF-8 (e.g. UTF-16 from PowerShell `>`):
    such a file must fail the guard instead of parsing to nothing and passing silently."""
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def secret_problems(text: str | bytes) -> list[str]:
    """Findings name the key and the rule only -- never the value itself. Fails closed on bytes
    that are not plain UTF-8."""
    if isinstance(text, bytes):
        decoded = read_env_bytes(text)
        if decoded is None:
            return ["file is not plain UTF-8 text (e.g. UTF-16): cannot be checked, refusing it"]
        text = decoded
    problems = []
    for key, value in parse(text).items():
        if SECRET_NAME.search(key) and value:
            problems.append(f"{key}: secret-named key must be empty in a tracked env file")
        for token in SEPARATORS.split(value):
            if len(token) >= MIN_TOKEN and entropy(token) > MAX_ENTROPY:
                problems.append(f"{key}: contains a high-entropy token ({len(token)} chars) - looks like a secret")
                break
    return problems


def tracked_env_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    return [p for p in out.splitlines() if re.search(r"(^|/)\.env([.\w-]*)$", p)]


def staged(path: str) -> bytes:
    """The staged (index) bytes of a tracked file. Raises if git can't produce them, so a failed
    lookup can never be mistaken for an empty, clean file."""
    r = subprocess.run(["git", "show", f":{path}"], cwd=REPO, capture_output=True)
    if r.returncode != 0:
        raise AssertionError(f"could not read the staged version of {path}")
    return r.stdout


# ---- the guard ------------------------------------------------------------------------------


def test_only_the_template_is_tracked() -> None:
    files = tracked_env_files()
    assert TEMPLATE in files
    assert not [f for f in files if f != TEMPLATE], f"unexpected tracked env files: {files}"


def test_template_contents_are_pinned_placeholders() -> None:
    for label, raw in (("working tree", (REPO / TEMPLATE).read_bytes()), ("index", staged(TEMPLATE))):
        text = read_env_bytes(raw)
        assert text is not None, f"{TEMPLATE} ({label}) is not plain UTF-8"
        got = parse(text)
        extra, missing = set(got) - set(EXPECTED), set(EXPECTED) - set(got)
        changed = sorted(k for k in set(got) & set(EXPECTED) if got[k] != EXPECTED[k])
        assert not (extra or missing or changed), (
            f"{TEMPLATE} ({label}) differs from the pinned placeholders: extra={sorted(extra)} "
            f"missing={sorted(missing)} changed={changed}. Real values belong in backend/.env."
        )


@pytest.mark.parametrize("path", ["tracked"])
def test_no_tracked_env_file_contains_a_secret(path: str) -> None:
    for f in tracked_env_files():
        for label, raw in (("working tree", (REPO / f).read_bytes()), ("index", staged(f))):
            assert secret_problems(raw) == [], f"{f} ({label}): {secret_problems(raw)}"


# ---- the guard must catch last session's mistake --------------------------------------------


def _with(key: str, value: str) -> str:
    text = (REPO / TEMPLATE).read_text(encoding="utf-8")
    return re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", text)


def test_catches_a_committed_api_token_like_last_session() -> None:
    fake = secrets.token_urlsafe(32)  # same shape as the leaked token: 43 url-safe chars, quoted
    bad = _with("API_TOKEN", f'"{fake}"')
    problems = secret_problems(bad)
    assert any(p.startswith("API_TOKEN: secret-named") for p in problems)
    assert any("high-entropy" in p for p in problems)
    assert all(fake not in p for p in problems)  # findings never echo the secret
    assert parse(bad)["API_TOKEN"] != EXPECTED["API_TOKEN"]  # and the pin fails too


@pytest.mark.parametrize("key,value", [
    ("OPENAI_API_KEY", "sk-proj-" + secrets.token_urlsafe(40)),
    ("SNAPSHOT_KEY", "Zm9vYmFyYmF6cXV4" + secrets.token_urlsafe(24)),  # Fernet-like
    ("DATABASE_URL", "postgresql+psycopg://admin:" + secrets.token_urlsafe(24) + "@db.internal:5432/prod"),
    ("TEST_SHEET_ID", "1" + secrets.token_urlsafe(32)),  # not a secret, but not a placeholder either
])
def test_catches_other_real_values(key: str, value: str) -> None:
    assert secret_problems(_with(key, value)), key


def test_placeholders_and_dev_defaults_pass() -> None:
    assert secret_problems((REPO / TEMPLATE).read_text(encoding="utf-8")) == []
    assert secret_problems("DATABASE_URL=postgresql+psycopg://sheets:sheets@localhost:5432/sheets\nX=\n") == []


def test_fails_closed_on_a_utf16_file_with_the_same_mistake() -> None:
    """A PowerShell-written (UTF-16) template must not parse to nothing and pass silently."""
    bad = _with("API_TOKEN", f'"{secrets.token_urlsafe(32)}"').encode("utf-16")
    assert secret_problems(bad) == ["file is not plain UTF-8 text (e.g. UTF-16): cannot be checked, refusing it"]
    assert read_env_bytes(bad) is None


def test_empty_input_is_not_silently_clean_for_the_pin() -> None:
    """The pin compares full key sets, so an empty or unparseable file fails it."""
    assert parse("") != EXPECTED
