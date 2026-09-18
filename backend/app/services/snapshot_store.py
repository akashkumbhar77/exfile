"""Encrypted, expiring snapshot storage (B.8).

Snapshot bodies (gzip JSON, the only place raw cell values may persist) are
Fernet-encrypted at rest and carry the id of the key that sealed them. Stored
snapshots older than `retention_days` are purged; restoring a purged run fails
with `SnapshotExpired`.

`LocalSnapshotStore` (per-run files) serves the S1 file-config CLI path;
`DbSnapshotStore` (Postgres `snapshots`, body_gz = ciphertext, key_id column)
serves registered sheets from S2 on. Both expose save/load.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.services.snapshots import Snapshot

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class SnapshotError(RuntimeError):
    pass


class SnapshotExpired(SnapshotError):
    pass


@dataclass(frozen=True)
class SnapshotCipher:
    key_id: str
    fernet: Fernet

    @staticmethod
    def from_key(key: str, key_id: str) -> SnapshotCipher:
        return SnapshotCipher(key_id, Fernet(key.encode("ascii")))

    def seal(self, body_gz: bytes) -> bytes:
        return self.fernet.encrypt(body_gz)

    def open(self, sealed: bytes, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise SnapshotError(f"snapshot sealed with key {key_id!r}; configured key is {self.key_id!r}")
        try:
            return self.fernet.decrypt(sealed)
        except InvalidToken as exc:
            raise SnapshotError("snapshot could not be decrypted with the configured key") from exc


class LocalSnapshotStore:
    def __init__(self, root: Path, cipher: SnapshotCipher, retention_days: int) -> None:
        self.root = root
        self.cipher = cipher
        self.retention = timedelta(days=retention_days)

    def _path(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise SnapshotError("invalid run id")
        return self.root / f"{run_id}.snap.json"

    def _purged_marker(self, run_id: str) -> Path:
        return self.root / f"{run_id}.purged"

    def save(self, run_id: str, sheet_ref: str, snapshots: list[Snapshot], now: datetime | None = None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        created = (now or datetime.now(UTC)).isoformat()
        record = {
            "run_id": run_id,
            "sheet_ref": sheet_ref,
            "created_at": created,
            "key_id": self.cipher.key_id,
            "snapshots": [
                {
                    "rule_id": s.rule_id,
                    "tab": s.tab,
                    "range_a1": s.range_a1,
                    "existed": s.existed,
                    "body": base64.b64encode(self.cipher.seal(s.body_gz)).decode("ascii"),
                }
                for s in snapshots
            ],
        }
        path = self._path(run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(path)  # atomic: a snapshot file is either complete or absent
        return path

    def load(self, run_id: str) -> tuple[str, list[Snapshot]]:
        path = self._path(run_id)
        if not path.exists():
            if self._purged_marker(run_id).exists():
                raise SnapshotExpired(
                    f"run {run_id} is older than the {self.retention.days}-day snapshot retention; "
                    "its snapshot was purged and it can no longer be undone"
                )
            raise SnapshotError(f"no snapshot stored for run {run_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        key_id = record["key_id"]
        snaps = [
            Snapshot(run_id, s["rule_id"], s["tab"], s["range_a1"], s["existed"],
                     self.cipher.open(base64.b64decode(s["body"]), key_id))
            for s in record["snapshots"]
        ]
        return record["sheet_ref"], snaps

    def purge_expired(self, now: datetime | None = None) -> list[str]:
        """Delete snapshots past retention; leave a tombstone so undo can explain why it fails."""
        if not self.root.exists():
            return []
        cutoff = (now or datetime.now(UTC)) - self.retention
        purged: list[str] = []
        for path in sorted(self.root.glob("*.snap.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if datetime.fromisoformat(record["created_at"]) < cutoff:
                run_id = record["run_id"]
                self._purged_marker(run_id).write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
                path.unlink()
                purged.append(run_id)
        return purged


class SnapshotStore(Protocol):
    def save(self, run_id: str, sheet_ref: str, snapshots: list[Snapshot], now: datetime | None = None) -> object: ...

    def load(self, run_id: str) -> tuple[str, list[Snapshot]]: ...


class DbSnapshotStore:
    """Postgres `snapshots` table (B.8). Each save commits on its own, before any sheet write."""

    def __init__(self, factory: sessionmaker[Session], cipher: SnapshotCipher) -> None:
        self.factory = factory
        self.cipher = cipher

    def save(self, run_id: str, sheet_ref: str, snapshots: list[Snapshot], now: datetime | None = None) -> int:
        from app.models import Sheet, SnapshotRow

        with self.factory() as s, s.begin():
            sheet = s.scalar(select(Sheet).where(Sheet.google_sheet_id == sheet_ref))
            if sheet is None:
                raise SnapshotError(f"sheet {sheet_ref} is not registered")
            for seq, snap in enumerate(snapshots):
                s.add(SnapshotRow(
                    org_id=sheet.org_id, sheet_id=sheet.id, run_id=run_id, seq=seq, rule_id=snap.rule_id,
                    tab=snap.tab, range_a1=snap.range_a1, existed=snap.existed,
                    body_gz=self.cipher.seal(snap.body_gz), key_id=self.cipher.key_id,
                    **({"created_at": now} if now else {}),
                ))
        return len(snapshots)

    def load(self, run_id: str) -> tuple[str, list[Snapshot]]:
        from app.models import Org, Sheet, SnapshotRow

        with self.factory() as s:
            rows = list(s.scalars(select(SnapshotRow).where(SnapshotRow.run_id == run_id).order_by(SnapshotRow.seq)))
            if not rows:
                raise SnapshotError(f"no snapshot stored for run {run_id}")
            sheet = s.get(Sheet, rows[0].sheet_id)
            assert sheet is not None
            if any(r.body_gz is None for r in rows):
                org = s.get(Org, rows[0].org_id)
                days = org.snapshot_retention_days if org else 0
                raise SnapshotExpired(
                    f"run {run_id} is older than the {days}-day snapshot retention; "
                    "its snapshot was purged and it can no longer be undone"
                )
            snaps = [
                Snapshot(run_id, r.rule_id, r.tab, r.range_a1, r.existed,
                         self.cipher.open(r.body_gz, r.key_id))  # type: ignore[arg-type]
                for r in rows
            ]
            return sheet.google_sheet_id, snaps


def purge_expired_snapshots(factory: sessionmaker[Session], now: datetime | None = None) -> int:
    """Daily job (B.8): drop snapshot bodies past each org's retention; rows stay as tombstones."""
    from app.models import Org, SnapshotRow

    now = now or datetime.now(UTC)
    purged = 0
    with factory() as s, s.begin():
        for org in s.scalars(select(Org)):
            cutoff = now - timedelta(days=org.snapshot_retention_days)
            result = s.execute(
                update(SnapshotRow)
                .where(SnapshotRow.org_id == org.id, SnapshotRow.created_at < cutoff,
                       SnapshotRow.body_gz.is_not(None))
                .values(body_gz=None, purged_at=now)
            )
            purged += int(getattr(result, "rowcount", 0) or 0)
    return purged
