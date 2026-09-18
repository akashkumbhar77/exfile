"""Privacy invariant B.6: raw cell values never reach logs, errors, run/event rows or metrics.

`Redacted` wraps cell contents wherever they cross the adapter boundary. Every
string conversion yields "<redacted>"; only `reveal()` returns the value, and it
is called solely inside the evaluator and the adapter write path.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

T = TypeVar("T")

REDACTED = "<redacted>"


class Redacted(Generic[T]):
    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def reveal(self) -> T:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Redacted) and bool(self._value == other._value)

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> Any:
        raise TypeError("Redacted values cannot be pickled")
