"""Dead-letter reason codes shared by the kernel, the bus and the reconciler.

One closed vocabulary (delivery contract section 2.5): every
``middleware_command_dead_letters.reason_code`` the platform writes is named
here, and the REEXECUTE waiver for a ``safe_reexecution=False`` adapter is
decided here from the stored record alone. The outcome certainty is derived
from ``reason_code`` + ``attempt_number`` + ``error_class``; it is never stored.
"""

from __future__ import annotations

from typing import Protocol

# attempt_number == 0: no attempt was ever opened, so no provider request exists.
TERMINAL_FAILURE = "terminal_failure"
EFFECT_GATE_DENIED = "effect_gate_denied"
DEFERRAL_EXHAUSTED = "deferral_exhausted"
DISPATCH_AGE_EXCEEDED = "dispatch_age_exceeded"

# attempt_number >= 1.
RETRY_EXHAUSTED = "retry_exhausted"
CANCEL_REQUESTED_PROVIDER_MISSING = "cancel_requested_provider_missing"
PROVIDER_MISSING_MANUAL_REPAIR = "provider_missing_manual_repair"
RECONCILIATION_EXHAUSTED = "reconciliation_exhausted"
READBACK_UNSUPPORTED = "readback_unsupported"
RECONCILIATION_DEAD_LETTER_UNRECORDED = "reconciliation_dead_letter_unrecorded"

# error_class of the one cancel dead letter written *before* the provider call
# (the bus re-read the command after opening the attempt and found a cancel).
# The reconciler's cancel dead letter (provider shows no record after a send
# may have happened) uses a different error_class and stays outcome-unknown.
CANCELLED_BEFORE_SEND = "cancelled_before_send"

NO_EFFECT_DEAD_LETTER_CODES = frozenset(
    {EFFECT_GATE_DENIED, DEFERRAL_EXHAUSTED, DISPATCH_AGE_EXCEEDED, TERMINAL_FAILURE}
)

DEAD_LETTER_REASON_CODES = NO_EFFECT_DEAD_LETTER_CODES | frozenset(
    {
        RETRY_EXHAUSTED,
        CANCEL_REQUESTED_PROVIDER_MISSING,
        PROVIDER_MISSING_MANUAL_REPAIR,
        RECONCILIATION_EXHAUSTED,
        READBACK_UNSUPPORTED,
        RECONCILIATION_DEAD_LETTER_UNRECORDED,
    }
)


class DeadLetterFacts(Protocol):
    attempt_number: int
    reason_code: str
    error_class: str
    poisoned: bool


def proven_not_sent(record: DeadLetterFacts, *, latest_attempt: int) -> bool:
    """True only when the stored dead letter proves no provider request exists.

    * attempt 0 with a no-effect code: no attempt was ever opened;
    * the bus's pre-send cancel (owner decision S5 Q1): the attempt was opened
      but the command was re-read and found cancelled before the provider call.

    ``latest_attempt`` must equal the recorded attempt, so no later attempt
    can have sent anything. Poisoned dead letters are never waived (S5 Q4).
    """
    if record.poisoned or latest_attempt != record.attempt_number:
        return False
    if record.attempt_number == 0:
        return record.reason_code in NO_EFFECT_DEAD_LETTER_CODES
    return (
        record.reason_code == CANCEL_REQUESTED_PROVIDER_MISSING
        and record.error_class == CANCELLED_BEFORE_SEND
    )


__all__ = [
    "CANCELLED_BEFORE_SEND",
    "CANCEL_REQUESTED_PROVIDER_MISSING",
    "DEAD_LETTER_REASON_CODES",
    "DEFERRAL_EXHAUSTED",
    "DISPATCH_AGE_EXCEEDED",
    "EFFECT_GATE_DENIED",
    "NO_EFFECT_DEAD_LETTER_CODES",
    "PROVIDER_MISSING_MANUAL_REPAIR",
    "READBACK_UNSUPPORTED",
    "RECONCILIATION_DEAD_LETTER_UNRECORDED",
    "RECONCILIATION_EXHAUSTED",
    "RETRY_EXHAUSTED",
    "TERMINAL_FAILURE",
    "proven_not_sent",
]
