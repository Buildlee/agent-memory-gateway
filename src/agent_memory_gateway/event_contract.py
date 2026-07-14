"""不可变记忆事件的校验、规范化和稳定哈希。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .auth import Principal
from .security import SecurityAssessment, SensitiveContentScanner


ALLOWED_SCOPES = frozenset({"user", "workspace", "device", "agent", "private"})
EVENT_TYPE_PROPOSED = "memory.proposed"


class EventValidationError(ValueError):
    """可作为稳定 API 错误码返回的事件格式错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SensitiveContentError(EventValidationError):
    """携带不含原文的分类结果，供 Gateway 写拒绝审计。"""

    def __init__(self, assessment: SecurityAssessment) -> None:
        super().__init__("SENSITIVE_CONTENT")
        self.assessment = assessment


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EventValidationError("EVENT_PAYLOAD_INVALID") from exc


def _required_text(payload: dict[str, Any], field: str, maximum: int) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise EventValidationError(f"{field.upper()}_REQUIRED")
    if len(value) > maximum:
        raise EventValidationError(f"{field.upper()}_TOO_LONG")
    return value


def _parse_occurred_at(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        raise EventValidationError("OCCURRED_AT_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError("OCCURRED_AT_INVALID") from exc
    if parsed.tzinfo is None:
        raise EventValidationError("OCCURRED_AT_INVALID")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProposedMemoryEvent:
    """已完成身份外部校验、可安全进入 Gateway 事件账本的候选事件。"""

    event_id: str
    device_seq: int
    occurred_at: str
    workspace_id: str
    session_id: str | None
    causation_id: str | None
    schema_version: int
    payload: dict[str, Any]

    @property
    def event_type(self) -> str:
        return EVENT_TYPE_PROPOSED

    def envelope(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "device_seq": self.device_seq,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "payload": self.payload,
            "causation_id": self.causation_id,
            "schema_version": self.schema_version,
        }

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.envelope())).hexdigest()

    def aad(self, principal: Principal) -> bytes:
        return (
            f"{principal.tenant_id}:{principal.user_id}:{principal.device_id}:"
            f"{principal.agent_installation_id}:{self.event_id}"
        ).encode("utf-8")


def parse_proposed_event(
    payload: dict[str, Any],
    principal: Principal,
    scanner: SensitiveContentScanner | None = None,
) -> ProposedMemoryEvent:
    """把 MCP/HTTP 输入变成不能携带自报身份的稳定事件。"""

    event_id = _required_text(payload, "event_id", 128)
    workspace_id = _required_text(payload, "workspace_id", 256)
    principal.require_workspace(workspace_id)
    raw_sequence = payload.get("device_seq")
    if isinstance(raw_sequence, bool):
        raise EventValidationError("DEVICE_SEQ_INVALID")
    try:
        device_seq = int(raw_sequence)
    except (TypeError, ValueError) as exc:
        raise EventValidationError("DEVICE_SEQ_REQUIRED") from exc
    if device_seq < 0:
        raise EventValidationError("DEVICE_SEQ_INVALID")

    schema_version = payload.get("schema_version", 1)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise EventValidationError("SCHEMA_VERSION_UNSUPPORTED")
    if payload.get("event_type") not in (None, EVENT_TYPE_PROPOSED):
        raise EventValidationError("EVENT_TYPE_UNSUPPORTED")

    content = _required_text(payload, "content", 20_000)
    scope = str(payload.get("scope") or "workspace")
    if scope not in ALLOWED_SCOPES:
        raise EventValidationError("SCOPE_UNSUPPORTED")
    kind = _required_text(payload | {"kind": payload.get("kind") or "note"}, "kind", 128)
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise EventValidationError("METADATA_INVALID")
    metadata_json = _canonical_json(metadata)
    if len(metadata_json) > 20_000:
        raise EventValidationError("METADATA_TOO_LARGE")
    assessment = (scanner or SensitiveContentScanner()).assess(
        (content, metadata_json.decode("utf-8"))
    )
    if assessment.has_sensitive_content:
        raise SensitiveContentError(assessment)

    try:
        confidence = float(payload.get("confidence", 0.72))
    except (TypeError, ValueError) as exc:
        raise EventValidationError("CONFIDENCE_INVALID") from exc
    if not 0 <= confidence <= 1:
        raise EventValidationError("CONFIDENCE_INVALID")
    evidence = str(payload.get("evidence") or "agent_observed").strip()
    if not evidence or len(evidence) > 128:
        raise EventValidationError("EVIDENCE_INVALID")
    session_id = str(payload.get("session_id") or "").strip() or None
    causation_id = str(payload.get("causation_id") or "").strip() or None
    if (session_id and len(session_id) > 256) or (causation_id and len(causation_id) > 128):
        raise EventValidationError("EVENT_REFERENCE_TOO_LONG")

    return ProposedMemoryEvent(
        event_id=event_id,
        device_seq=device_seq,
        occurred_at=_parse_occurred_at(payload.get("occurred_at")),
        workspace_id=workspace_id,
        session_id=session_id,
        causation_id=causation_id,
        schema_version=schema_version,
        payload={
            "content": content,
            "kind": kind,
            "requested_scope": scope,
            "evidence": evidence,
            "confidence": confidence,
            "metadata": metadata,
            "instruction_like": assessment.instruction_like,
            "instruction_rule_ids": list(assessment.instruction_rule_ids),
            "security_rule_version": assessment.rule_version,
        },
    )
