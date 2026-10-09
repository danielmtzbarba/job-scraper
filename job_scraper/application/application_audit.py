"""Content-free audit metadata for job application attempts.

Do not add arbitrary payloads, labels, URLs, prompts, answers, or exception text here.
The fixed schema is an allowlist shared by the service and repository.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field


AuditActor = Literal["web", "mcp", "adk", "internal"]
AuditOutcome = Literal["started", "succeeded", "denied", "failed", "uncertain"]
AuditEventType = Literal[
    "attempt_started", "page_observed", "cookie_choice", "model_decision",
    "browser_action", "answer_saved", "consent_approved", "review_prepared",
    "review_approved", "submission_claimed", "submission_result",
    "submission_confirmed", "attempt_discarded", "artifact_erased",
    "state_changed",
]

_actor: ContextVar[AuditActor | None] = ContextVar("application_audit_actor", default=None)
_request: ContextVar[str | None] = ContextVar("application_audit_request", default=None)
logger = structlog.get_logger(__name__)


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    event_type: AuditEventType
    outcome: AuditOutcome
    actor_kind: AuditActor
    request_id: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    action_kind: Literal["click", "fill", "select", "scroll", "pause"] | None = None
    target_id: str | None = Field(default=None, pattern=r"^c[0-9]{1,5}$")
    review_version: int | None = Field(default=None, ge=0)
    model_id: str | None = Field(default=None, max_length=100)
    prompt_tokens: int | None = Field(default=None, ge=0)
    candidate_tokens: int | None = Field(default=None, ge=0)


@contextmanager
def audit_scope(actor_kind: AuditActor, request_id: str | None = None) -> Iterator[None]:
    actor_token = _actor.set(actor_kind)
    request_token = _request.set(request_id or uuid4().hex)
    try:
        yield
    finally:
        _request.reset(request_token)
        _actor.reset(actor_token)


def event(attempt_id: str, event_type: AuditEventType, outcome: AuditOutcome,
          *, default_actor: AuditActor = "internal", **fields: object) -> AuditEvent:
    return AuditEvent(
        attempt_id=attempt_id, event_type=event_type, outcome=outcome,
        actor_kind=_actor.get() or default_actor, request_id=_request.get(), **fields,
    )


def log_event(audit_event: AuditEvent, event_id: str) -> None:
    """Operational pointer to a committed audit row; excludes private material."""
    logger.info(
        "application_audit_event", event_id=event_id,
        attempt_id=audit_event.attempt_id, event_type=audit_event.event_type,
        outcome=audit_event.outcome, actor_kind=audit_event.actor_kind,
        request_id=audit_event.request_id,
    )
