"""Lifecycle policy for the agent provisioning saga.

Pure, database-free rules shared by ``app.api.v1.agent_provisioning`` (the
request-path saga) and ``app.workers.agent_provisioning_reconciler`` (the
crash-recovery sweeper):

* which lifecycle action may run from which saga state;
* which states mean "a saga run currently owns this row" and when that
  ownership (an ``updated_at``-based lease) has expired;
* which provider operation is undone by which inverse operation, so a
  resumed saga re-issues exactly the calls whose effect is no longer live;
* how a secret-safe VICIdial transport error maps onto a stable,
  Odoo-visible error code and whether retrying it can help;
* the Prometheus series every transition and step increments.

Nothing here performs I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from prometheus_client import Counter

# A saga run is executing (or crashed while executing) in these states.
IN_FLIGHT_STATES = frozenset(
    {
        "REQUESTED",
        "VALIDATING",
        "IDENTITY",
        "ENTITLEMENTS",
        "CHANNEL_PROVISIONING",
        "READBACK",
        "RECONCILING",
    }
)
# A saga run finished and recorded an honest outcome.
SETTLED_STATES = frozenset({"EFFECTIVE", "PARTIAL", "FAILED"})

# action -> states it may start from. An in-flight row whose lease expired
# (its runner crashed) is additionally accepted by every action except
# reactivate - see ``action_allowed``.
LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    # Re-run the provisioning saga. Never from SUSPENDED: reconciling a
    # suspended person would silently re-provision access that an operator
    # deliberately removed - reactivate is the only way back.
    "reconcile": SETTLED_STATES,
    # SUSPENDED is accepted so a suspension whose deprovision steps failed
    # can be retried; it is a no-op once every step has succeeded.
    "suspend": SETTLED_STATES | {"SUSPENDED"},
    "reactivate": frozenset({"SUSPENDED"}),
    # Revoke is idempotent and must always be reachable for offboarding.
    "revoke": SETTLED_STATES | {"SUSPENDED", "REVOKED"},
}

# (system, operation) -> the operations that undo its effect.
# reserve/adopt have no inverse: Vicidialer-Codestra exposes no approved
# extension-release route (see app.adapters.vicidial.mtls_client), so an
# extension stays bound to its VICIdial user until released out of band.
INVERSE_OPERATIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("vicidial", "sync_agent"): ("disable_agent",),
    ("vicidial", "provision_webrtc"): ("revoke_webrtc",),
    ("keycloak", "disable_user"): ("enable_user",),
}

# Failure codes a later reconcile can plausibly clear without an operator
# changing configuration or provider state first.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "VICIDIAL_UNAVAILABLE",
        "VICIDIAL_ADAPTER_ERROR",
        "KEYCLOAK_ADAPTER_ERROR",
        "KLYROW_ADAPTER_ERROR",
        "TELNEXA_ADAPTER_ERROR",
    }
)

TRANSITIONS = Counter(
    "codestra_agent_provisioning_transitions_total",
    "Agent provisioning lifecycle transitions",
    ["action", "from_state", "to_state"],
)
STEPS = Counter(
    "codestra_agent_provisioning_steps_total",
    "Agent provisioning provider step outcomes",
    ["system", "operation", "state"],
)
CONFLICTS = Counter(
    "codestra_agent_provisioning_conflicts_total",
    "Agent provisioning requests refused to protect a binding or a running saga",
    ["kind"],
)
STALE_RECOVERED = Counter(
    "codestra_agent_provisioning_stale_recovered_total",
    "In-flight agent provisioning sagas resumed after their lease expired",
)
STALE_RECOVERY_FAILED = Counter(
    "codestra_agent_provisioning_stale_recovery_failed_total",
    "Abandoned agent provisioning sagas whose resumption raised and was skipped",
)


def lease_active(state: str, updated_at: datetime, *, now: datetime, lease_seconds: int) -> bool:
    """True while a saga run still owns the row.

    The saga commits (and refreshes ``updated_at``) after every step, so a
    row that has sat in an in-flight state for longer than one lease was
    abandoned by a crashed runner and may be resumed.
    """
    return state in IN_FLIGHT_STATES and updated_at > now - timedelta(seconds=lease_seconds)


def action_allowed(action: str, state: str, *, lease_is_active: bool) -> bool:
    if state in IN_FLIGHT_STATES:
        return not lease_is_active and action != "reactivate"
    return state in LIFECYCLE_TRANSITIONS.get(action, frozenset())


def classify_vicidial_error(exc: BaseException, *, conflict_code: str) -> str:
    """Map a ``VicidialMtlsError`` onto a stable error code.

    The adapter deliberately raises one secret-safe exception type and
    chains the transport cause, so the status class is read from
    ``__cause__``; the response body is never inspected or echoed.
    """
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        if status == 409:
            return conflict_code
        if status in (401, 403):
            return "VICIDIAL_UNAUTHORIZED"
        if 400 <= status < 500:
            return "VICIDIAL_REJECTED"
        return "VICIDIAL_UNAVAILABLE"
    if isinstance(cause, (httpx.TimeoutException, httpx.NetworkError)):
        return "VICIDIAL_UNAVAILABLE"
    return "VICIDIAL_ADAPTER_ERROR"


def is_retryable(error_code: str | None) -> bool:
    return error_code in RETRYABLE_ERROR_CODES
