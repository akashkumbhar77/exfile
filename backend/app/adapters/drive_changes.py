"""Fleet-level Drive changes feed (SPEC-PATCH-002 A.1). The only Drive surface we use (B.9):

  * changes.getStartPageToken / changes.list -- the fleet feed
  * files.get(fields=modifiedTime) on a *registered* file -- self-write watermark (A.3)
  * lastModifyingUser.me on each change -- recognizes our own writes by author, because live
    Drive reports a write's modifiedTime only minutes later (DECISIONS: S2 live findings)

Never files.list / search. Two requests per poll regardless of fleet size
(one list page per poll in steady state).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.adapters.base import SourceRegistry, UnregisteredSource

DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.metadata.readonly",)
_CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,time,file(modifiedTime,lastModifyingUser(me)))"
)


@dataclass(frozen=True)
class FileChange:
    file_id: str
    time: datetime  # when the change was recorded
    modified_time: datetime | None  # file modifiedTime at that change, if known
    removed: bool = False
    by_self: bool = False  # lastModifyingUser.me: the service account itself made this change


def parse_rfc3339(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class DriveChangesFeed:
    def __init__(self, service: Any, registry: SourceRegistry) -> None:
        self._svc = service
        self._registry = registry

    @classmethod
    def from_service_account(cls, key_path: str, registry: SourceRegistry) -> DriveChangesFeed:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(key_path, scopes=list(DRIVE_SCOPES))  # type: ignore[no-untyped-call]
        return cls(build("drive", "v3", credentials=creds, cache_discovery=False), registry)

    def start_page_token(self) -> str:
        resp = self._svc.changes().getStartPageToken(supportsAllDrives=True).execute()
        return str(resp["startPageToken"])

    def list_changes(self, page_token: str) -> tuple[list[FileChange], str]:
        """All changes since `page_token`; returns (changes, new start token to persist)."""
        changes: list[FileChange] = []
        token = page_token
        while True:
            resp = self._svc.changes().list(
                pageToken=token, fields=_CHANGE_FIELDS, pageSize=1000, spaces="drive",
                includeItemsFromAllDrives=True, supportsAllDrives=True, includeRemoved=True,
            ).execute()
            changes.extend(_parse(resp.get("changes", [])))
            if "newStartPageToken" in resp:
                return changes, str(resp["newStartPageToken"])
            token = str(resp["nextPageToken"])

    def modified_time(self, file_id: str) -> datetime:
        if not self._registry.is_registered(file_id):
            raise UnregisteredSource(f"file {file_id!r} is not in the registry; refusing to open it")
        resp = self._svc.files().get(fileId=file_id, fields="modifiedTime", supportsAllDrives=True).execute()
        return parse_rfc3339(resp["modifiedTime"])


def _parse(raw: list[dict[str, Any]]) -> Iterator[FileChange]:
    for c in raw:
        if "fileId" not in c:
            continue  # shared-drive level changes carry no file
        f = c.get("file", {})
        mt = f.get("modifiedTime")
        yield FileChange(
            file_id=str(c["fileId"]),
            time=parse_rfc3339(c["time"]),
            modified_time=parse_rfc3339(mt) if mt else None,
            removed=bool(c.get("removed", False)),
            by_self=bool(f.get("lastModifyingUser", {}).get("me", False)),
        )
