"""What each compilation target can express (SPEC-PATCH-004 B, as data not prose).

PATCH-004 A.1: rule semantics are defined once in ConfigSpec; targets are emitters compiled from
it. An emitter never invents behaviour. A config carrying anything the target cannot express is
refused, rule by rule, in the same shape as ConfigSpec validation errors (json-pointer + reason),
and nothing is emitted (A.3: silent partial emission is a build failure).

Two axes, deliberately separate:
  * `in_matrix` - does PATCH-004 B say this target will ever support it?
  * `emitted`   - does the emitter produce it today? (milestone status)
Both produce a refusal, with different reasons, so an owner can tell "never" from "not yet".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.config import ConfigSpec, Rule
from app.services.validator import ValidationIssue

TARGET = "apps_script"
MILESTONE = "S7a"


@dataclass(frozen=True)
class Support:
    in_matrix: bool
    emitted: bool
    note: str = ""

    def refusal(self, what: str) -> str | None:
        if not self.in_matrix:
            return f"the {TARGET} target does not support {what}: {self.note}"
        if not self.emitted:
            return f"{what} is not emitted yet ({self.note})"
        return None


_YES = Support(True, True)


def _later(note: str) -> Support:
    """In the target's matrix, but not emitted by this milestone yet."""
    return Support(True, False, note)


# PATCH-004 B. "Supported" is the patch's list; move/copy are in neither of the patch's lists, so
# they are refused rather than guessed at (see docs/BLOCKERS.md).
ACTIONS: dict[str, Support] = {
    "sort": _YES,
    "format": _YES,
    "consolidate": _later("S7b; single file, cross-tab only"),
    "validate": _later("S7b"),
    "dedupe": _later("S7b"),
    "clear": _later("S7b"),
    "move": Support(False, False, "PATCH-004 B lists neither move nor copy for this target"),
    "copy": Support(False, False, "PATCH-004 B lists neither move nor copy for this target"),
}

TRIGGERS: dict[str, Support] = {
    "on_edit": _YES,
    "after": _YES,
    "debounced": _later("S7b; dirty flag + time trigger"),
    "schedule": Support(False, False, "PATCH-004 B lists on_edit, on_open, after and debounced only"),
}

# Capabilities the managed tier has that a pasted script structurally cannot, kept here so the
# download UI (E.2) and the refusals read from one table.
ABSENT_BY_DESIGN: dict[str, str] = {
    # D.5: the optional backup tab is the script tier's only undo and is not a snapshot.
    "snapshots_and_undo": "No undo. The managed service snapshots every run and can put a sheet back; "
                          "this script cannot.",
    "drift_repair": "No repair. If your headers change, this script stops and tells you, but it "
                    "cannot propose a fix.",
    "central_audit": "No weekly audit of whether the rules still match how you work.",
    "cross_file_rules": "Nothing across files: it only touches tabs in this one spreadsheet.",
    # A.4: no phone-home, which is exactly why there is no fleet view for this tier.
    "fleet_view": "No dashboard or history. The script reports to you in the sheet and to nobody else.",
}


def _issue(pointer: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(pointer=pointer, code=code, message=message)


def _trigger_kind(rule: Rule) -> str:
    return next(iter(rule.trigger.model_dump(by_alias=True)))


def check_config(config: ConfigSpec) -> list[ValidationIssue]:
    """Every reason this config cannot be emitted for the apps_script target, in config order."""
    out: list[ValidationIssue] = []
    for i, rule in enumerate(config.rules):
        support = ACTIONS.get(rule.action, Support(False, False, "unknown action"))
        reason = support.refusal(f"the {rule.action!r} action")
        if reason is not None:
            out.append(_issue(f"/rules/{i}", "unsupported_action", f"rule {rule.id!r}: {reason}"))
            continue  # one refusal per rule is enough to stop the build
        kind = _trigger_kind(rule)
        trigger = TRIGGERS.get(kind, Support(False, False, "unknown trigger"))
        reason = trigger.refusal(f"the {kind!r} trigger")
        if reason is not None:
            out.append(_issue(f"/rules/{i}/trigger", "unsupported_trigger", f"rule {rule.id!r}: {reason}"))
    return out
