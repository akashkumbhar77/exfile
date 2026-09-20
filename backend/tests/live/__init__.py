"""Test-only Google access, kept for the live proof (PATCH-005 P4 / PATCH-004 D.3).

Nothing in `app/` may import this package: `tests/test_google_fence.py` enforces it. These
modules are not part of the product, which never connects to anyone's spreadsheets.
"""
