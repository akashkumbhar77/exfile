"""Runtime settings from environment / .env (no secrets in code)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_application_credentials: Path | None = None
    # S1 registry (B.9): the adapter opens only these spreadsheet ids. S2 replaces this with
    # the `sheets` table.
    enrolled_sheet_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    test_sheet_id: str | None = None

    # B.8: Fernet key for snapshot bodies; key id recorded next to every snapshot.
    snapshot_key: str | None = None
    snapshot_key_id: str = "k1"
    snapshot_dir: Path = Path("var/snapshots")
    snapshot_retention_days: int = 30

    poll_interval_seconds: int = 30

    @field_validator("enrolled_sheet_ids", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def registered_ids(self) -> frozenset[str]:
        ids = set(self.enrolled_sheet_ids)
        if self.test_sheet_id:
            ids.add(self.test_sheet_id)
        return frozenset(ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()
