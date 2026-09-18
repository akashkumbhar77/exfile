"""Logging setup. Log lines carry addresses, counts, hashes and ids -- never cell contents (B.6)."""

from __future__ import annotations

import logging
import sys


def configure(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("googleapiclient", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
