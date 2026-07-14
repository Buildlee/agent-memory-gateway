"""Gateway 依赖的通用长期记忆后端协议。"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    def health(self) -> bool: ...

    def schema_version(self) -> str: ...

    def upsert_confirmed(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        content: str,
        kind: str,
        confidence: float,
        allow_instruction_like: bool = False,
    ) -> str: ...

    def get_by_refs(self, references: Iterable[str]) -> list[Any]: ...

    def search(
        self, *, allowed_references: Iterable[str], query: str, limit: int = 8
    ) -> list[Any]: ...

    def supersede(self, *, idempotency_key: str, old_ref: str, new_ref: str) -> str: ...

    def archive(self, *, idempotency_key: str, reference: str) -> str: ...

    def restore_superseded(
        self, *, idempotency_key: str, old_ref: str, new_ref: str
    ) -> str: ...

    def reactivate(self, *, idempotency_key: str, reference: str) -> str: ...

    def tombstone(
        self, *, idempotency_key: str, reference: str, deleted_revision: int
    ) -> str: ...

    def rebuild_crystal(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        source_refs: Iterable[str],
        scope_binding_hash: str,
    ) -> str: ...
