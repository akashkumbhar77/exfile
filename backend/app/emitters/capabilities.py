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

import re
from dataclasses import dataclass

from app.schemas.config import ConfigSpec, Rule, ScheduleTrigger
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
    # PATCH-005 I.7: in-file move/copy are supported ("move completed rows to Archive"); only
    # rules crossing spreadsheet files are refused, and the schema cannot express those yet.
    "move": _later("S7b; in-file only, and it forces the backup tab on"),
    "copy": _later("S7b; in-file only"),
}

# Rules that rewrite or remove rows. This tier has no snapshots, so PATCH-005 I.7 forces the
# backup tab on for them rather than leaving it optional (PATCH-004 D.5 default-off is overridden).
DESTRUCTIVE_ACTIONS = frozenset({"move", "clear", "dedupe"})

TRIGGERS: dict[str, Support] = {
    "on_edit": _YES,
    "after": _YES,
    "debounced": _later("S7b; dirty flag + time trigger"),
    # PATCH-005 I.7: time-driven triggers are native to Apps Script, hourly or coarser.
    "schedule": _later("S7b; hourly granularity or coarser, in the script's own timezone"),
}

# A cron minute field naming one fixed minute ("0", "30") runs at most hourly; a wildcard, a
# step, a list or a range in that field means something finer, which the target refuses.
HOURLY_OR_COARSER = re.compile(r"^\d{1,2}$")

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
    """Every reason this config cannot be emitted for the apps_script target, in config order.

    All of them, not just the first: "the action isn't emitted yet" and "this rule needs the
    backup tab on" are different problems, and an owner should see both at once.
    """
    out: list[ValidationIssue] = []
    for i, rule in enumerate(config.rules):
        support = ACTIONS.get(rule.action, Support(False, False, "unknown action"))
        reason = support.refusal(f"the {rule.action!r} action")
        if reason is not None:
            out.append(_issue(f"/rules/{i}", "unsupported_action", f"rule {rule.id!r}: {reason}"))

        kind = _trigger_kind(rule)
        trigger = TRIGGERS.get(kind, Support(False, False, "unknown trigger"))
        reason = trigger.refusal(f"the {kind!r} trigger")
        if reason is not None:
            out.append(_issue(f"/rules/{i}/trigger", "unsupported_trigger", f"rule {rule.id!r}: {reason}"))

        # Granularity is a property of the target, not of the milestone, so it is checked even
        # while schedule triggers are still unemitted.
        if isinstance(rule.trigger, ScheduleTrigger):
            minute = rule.trigger.schedule.cron.split()[0]
            if not HOURLY_OR_COARSER.match(minute):
                out.append(_issue(
                    f"/rules/{i}/trigger", "schedule_too_frequent",
                    f"rule {rule.id!r}: a generated script can run hourly at most; "
                    f"the minute field {minute!r} asks for something more frequent"))

        if rule.action in DESTRUCTIVE_ACTIONS and not config.guards.backup_tab:
            out.append(_issue(
                "/guards/backup_tab", "backup_tab_required",
                f"rule {rule.id!r} moves or removes rows and this tier has no undo, so "
                f"guards.backup_tab must be on"))
    return out
