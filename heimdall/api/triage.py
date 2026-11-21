"""Alert triage transition rules. Pure functions, no I/O."""

from __future__ import annotations

from datetime import datetime

from heimdall.schemas import AlertStatus

S = AlertStatus

# Statuses an analyst may set; NEW and EXPIRED are pipeline-owned.
_OPEN = {S.NEW, S.ACKNOWLEDGED, S.UNDER_REVIEW, S.SHELVED}
# 042 workflow: new -> acknowledged (owned) -> confirmed (real) -> resolved (activity over);
# any open state -> dismissed (false alarm, reason) or shelved (until a time, reason).
ALLOWED: dict[AlertStatus, set[AlertStatus]] = {
    S.NEW: {S.ACKNOWLEDGED, S.UNDER_REVIEW, S.CONFIRMED, S.DISMISSED, S.SHELVED},
    S.ACKNOWLEDGED: {S.UNDER_REVIEW, S.CONFIRMED, S.RESOLVED, S.DISMISSED, S.SHELVED},
    S.UNDER_REVIEW: {S.ACKNOWLEDGED, S.CONFIRMED, S.RESOLVED, S.DISMISSED, S.SHELVED},
    S.SHELVED: {S.ACKNOWLEDGED, S.UNDER_REVIEW, S.CONFIRMED, S.DISMISSED},
    S.CONFIRMED: {S.RESOLVED, S.UNDER_REVIEW},  # close when the activity ends, or reopen
    S.RESOLVED: {S.UNDER_REVIEW},  # reopen
    S.DISMISSED: {S.UNDER_REVIEW},  # reopen
    S.EXPIRED: set(),
}
NEEDS_REASON = {S.DISMISSED, S.SHELVED}


def validate(
    cur: AlertStatus,
    to: AlertStatus,
    reason: str | None,
    shelve_until: datetime | None,
    now: datetime,
) -> str | None:
    """Return an error message, or None if the transition is valid."""
    if to not in ALLOWED[cur]:
        return f"transition {cur.value} -> {to.value} not allowed"
    if to in NEEDS_REASON and not (reason and reason.strip()):
        return f"a reason is required to set status {to.value}"
    if to == S.SHELVED:
        if shelve_until is None:
            return "shelve_until is required when shelving"
        if shelve_until <= now:
            return "shelve_until must be in the future"
    elif shelve_until is not None:
        return "shelve_until only applies to shelved"
    return None
