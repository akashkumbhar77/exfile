"""Where a compile session's by-products go.

The managed tier wrote profiles, model-call metering and proposed configs to Postgres. Under
PATCH-005 the product stores nothing server-side (I.5): a compile happens in one process, the
config goes to the person with their script, and the rest is kept only long enough to report on.

`MemoryRecorder` is that default. The protocol stays so a caller that genuinely needs durable
records (the parked managed tier, a future evaluation harness) can supply its own without the
compile path knowing the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.schemas.config import ConfigSpec


@dataclass(frozen=True)
class Proposed:
    """What the compile path needs back after proposing: an id to refer to, and a version."""

    id: int
    version: int


class CompileRecorder(Protocol):
    def start(self, sheet_ref: str) -> int: ...

    def save_profile(self, source_id: int, body: dict[str, Any]) -> None: ...

    def save_llm_call(self, source_id: int, fields: dict[str, Any]) -> None: ...

    def propose(self, source_id: int, config: ConfigSpec, governed_headers: dict[str, list[str]],
                source: dict[str, Any]) -> Proposed: ...

    def event(self, source_id: int, kind: str, payload: dict[str, Any]) -> None: ...


@dataclass
class MemoryRecorder:
    """Keeps this session's artefacts in memory, and nowhere else."""

    sources: dict[str, int] = field(default_factory=dict)
    profiles: dict[int, dict[str, Any]] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    configs: dict[int, ConfigSpec] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def start(self, sheet_ref: str) -> int:
        return self.sources.setdefault(sheet_ref, len(self.sources) + 1)

    def save_profile(self, source_id: int, body: dict[str, Any]) -> None:
        self.profiles[source_id] = body

    def save_llm_call(self, source_id: int, fields: dict[str, Any]) -> None:
        self.llm_calls.append({"source_id": source_id, **fields})

    def propose(self, source_id: int, config: ConfigSpec, governed_headers: dict[str, list[str]],
                source: dict[str, Any]) -> Proposed:
        config_id = len(self.configs) + 1
        self.configs[config_id] = config
        return Proposed(config_id, 1)

    def event(self, source_id: int, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))
