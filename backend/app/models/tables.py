"""Registry schema (S2). `org_id` on every table (invariant 10).

Privacy (B.6): no column here holds raw cell values except `snapshots.body`,
which is Fernet ciphertext (B.8). Runs/events store ids, counts, hashes, addresses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)  # == id; uniform scoping column
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    created_at: Mapped[datetime] = _now()


class Sheet(Base):
    __tablename__ = "sheets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    google_sheet_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    # ACTIVE | PAUSED_DRIFT | PAUSED
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")
    active_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("configs.id", use_alter=True, name="fk_sheets_active_config"), nullable=True
    )
    # PATCH-002 A.2: per-tab values fingerprint of the state after the last completed run
    last_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # PATCH-002 A.3: Drive modifiedTime produced by our own last write
    self_write_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Config(Base):
    __tablename__ = "configs"
    __table_args__ = (UniqueConstraint("sheet_id", "version"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # PENDING_APPROVAL | ACTIVE | SUPERSEDED | REJECTED
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # required on reject
    # PATCH-003 decision (c): owner-supplied SUMMARY title, set at approval, never model-read
    summary_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # header text of each governed tab when proposed (structure only): drift "what changed" view
    governed_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {"kind": "onboarding", "session_id", "model", "prompt_version"} | {"kind": "cli"}
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _now()


class Run(Base):
    """One row per rule execution (or one per run for drift/no-op/error), as SPEC §3."""

    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rows_affected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # OK | NOOP | PAUSED_DRIFT | BLOCKED | ERROR | STALE | UNDONE
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _now()


class SnapshotRow(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # capture order within the run
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tab: Mapped[str] = mapped_column(String(200), nullable=False)
    range_a1: Mapped[str] = mapped_column(String(64), nullable=False)
    existed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    body_gz: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # Fernet(gzip JSON); NULL once purged
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int | None] = mapped_column(ForeignKey("sheets.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = _now()


class WatchState(Base):
    __tablename__ = "watch_state"
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    page_token: Mapped[str] = mapped_column(String(256), nullable=False)
    last_purge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Flag(Base):
    """Class B/C flags. Schema only in the pilot (SPEC-PATCH-001 B): no behavior yet."""

    __tablename__ = "flags"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), nullable=False)
    flag_class: Mapped[str] = mapped_column("class", String(1), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    proposed_config_id: Mapped[int | None] = mapped_column(ForeignKey("configs.id"), nullable=True)
    created_at: Mapped[datetime] = _now()


class Profile(Base):
    """Structural description of a sheet (B.7: structure + enum distributions, never row values)."""

    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("sheet_id", "version"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


class LlmCall(Base):
    """One row per LLM request (SPEC §6 "log every call"). Metadata only: no prompt or reply text."""

    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    sheet_id: Mapped[int | None] = mapped_column(ForeignKey("sheets.id"), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # onboarding | repair | audit
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_calls: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")  # tool names only
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # OK | ERROR
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _now()
