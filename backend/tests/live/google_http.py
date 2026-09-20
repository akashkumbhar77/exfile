"""Thread-safe Google API calls.

httplib2 (under googleapiclient) is not thread-safe, and the API serves requests from a thread
pool: the Approvals page fetches the readback and the preview at the same time, both reading the
sheet. On 2026-09-18 one of two concurrent live requests failed with a TLS read timeout during a
token refresh -- shared-Http races are the likely cause (a plain network timeout is possible too).
Either way, sharing is unsafe: per googleapiclient's guidance each thread gets its own authorized
Http; the credentials object is shared. Reads use `num_retries` (429/5xx, transient socket errors);
writes pass retries=0 -- a batchUpdate is not guaranteed un-applied on 5xx and some of its requests
(addSheet, appendDimension, addProtectedRange) are not idempotent.
"""

from __future__ import annotations

import threading
from typing import Any

DEFAULT_TIMEOUT_S = 60
DEFAULT_RETRIES = 2


class ThreadHttp:
    def __init__(self, credentials: Any, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._local = threading.local()

    def get(self) -> Any:
        http = getattr(self._local, "http", None)
        if http is None:
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]

            http = AuthorizedHttp(self._credentials, http=httplib2.Http(timeout=self._timeout))
            self._local.http = http
        return http


def execute(request: Any, thread_http: ThreadHttp | None, retries: int = DEFAULT_RETRIES) -> Any:
    """Run a googleapiclient request on this thread's own Http (fakes in tests take no args)."""
    if thread_http is None:
        return request.execute()
    return request.execute(http=thread_http.get(), num_retries=retries)
