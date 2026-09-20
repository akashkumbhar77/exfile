"""Per-thread Google HTTP clients and upstream-error mapping (live finding 2026-09-18)."""

from __future__ import annotations

import threading
from typing import Any

from tests.live.google_http import ThreadHttp, execute


class _Creds:  # minimal stand-in: AuthorizedHttp only stores it until a request is made
    def before_request(self, *a: Any, **k: Any) -> None: ...


def test_each_thread_gets_its_own_http_and_reuses_it() -> None:
    th = ThreadHttp(_Creds())
    main_a, main_b = th.get(), th.get()
    seen: list[Any] = []
    t = threading.Thread(target=lambda: seen.append(th.get()))
    t.start()
    t.join()
    assert main_a is main_b and seen[0] is not main_a


class _Req:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def execute(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "ok"


def test_reads_retry_and_writes_never_do() -> None:
    th = ThreadHttp(_Creds())
    read, write = _Req(), _Req()
    execute(read, th)
    execute(write, th, retries=0)
    assert read.kwargs["num_retries"] == 2 and write.kwargs["num_retries"] == 0
    assert read.kwargs["http"] is th.get()


def test_writes_in_the_adapter_pass_zero_retries() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent / "sheets_adapter.py").read_text(encoding="utf-8")
    call = src[src.index("batchUpdate(spreadsheetId=source_ref"):]
    assert "retries=0" in call[: call.index("\n\n")]
