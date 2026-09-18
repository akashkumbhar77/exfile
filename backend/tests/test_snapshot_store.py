"""B.8: snapshot bodies are encrypted at rest, carry a key id, and expire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.services.grid import Workbook
from app.services.snapshot_store import LocalSnapshotStore, SnapshotCipher, SnapshotError, SnapshotExpired
from app.services.snapshots import decode_tab, take_snapshot

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _store(root: Path, key: str | None = None, key_id: str = "k1", days: int = 30) -> LocalSnapshotStore:
    return LocalSnapshotStore(root, SnapshotCipher.from_key(key or Fernet.generate_key().decode(), key_id), days)


def test_bodies_encrypted_at_rest_and_round_trip(tmp_path: Path, workbook: Workbook) -> None:
    store = _store(tmp_path)
    tab = workbook.tab("MACHINES")
    assert tab is not None
    store.save("run_1", "sheet", [take_snapshot("run_1", "sort", tab.name, tab)], now=T0)
    on_disk = (tmp_path / "run_1.snap.json").read_bytes()
    for secret in (b"Acme", b"Lathe", b"Grinder"):
        assert secret not in on_disk
    assert b'"key_id": "k1"' in on_disk
    ref, snaps = store.load("run_1")
    assert ref == "sheet"
    restored = decode_tab(snaps[0].body_gz)
    assert restored.values == tab.values and restored.formats == tab.formats


def test_wrong_key_or_key_id_fails_clearly(tmp_path: Path, workbook: Workbook) -> None:
    key = Fernet.generate_key().decode()
    tab = workbook.tabs[1]
    _store(tmp_path, key).save("run_1", "s", [take_snapshot("run_1", "r", tab.name, tab)], now=T0)
    with pytest.raises(SnapshotError, match="could not be decrypted"):
        _store(tmp_path).load("run_1")
    with pytest.raises(SnapshotError, match="sealed with key 'k1'"):
        _store(tmp_path, key, key_id="k2").load("run_1")


def test_purge_expired_then_undo_fails_with_clear_message(tmp_path: Path, workbook: Workbook) -> None:
    store = _store(tmp_path, days=30)
    tab = workbook.tabs[1]
    store.save("run_old", "s", [take_snapshot("run_old", "r", tab.name, tab)], now=T0)
    store.save("run_new", "s", [take_snapshot("run_new", "r", tab.name, tab)], now=T0 + timedelta(days=20))
    assert store.purge_expired(now=T0 + timedelta(days=31)) == ["run_old"]
    with pytest.raises(SnapshotExpired, match="30-day snapshot retention"):
        store.load("run_old")
    store.load("run_new")  # still within retention
    with pytest.raises(SnapshotError, match="no snapshot stored"):
        store.load("run_never")


def test_run_id_cannot_escape_store(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        _store(tmp_path).load("../../etc/passwd")
