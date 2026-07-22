"""端侧个性化记忆的只读 Provider 与受控共享服务。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .security import SensitiveContentScanner


AUTO_SHARE_KINDS = frozenset(
    {"user_preference", "project_decision", "stable_fact", "long_term_convention"}
)
SUPPORTED_FILE_SUFFIXES = frozenset({".md", ".json", ".jsonl"})
MAX_PROVIDER_CONFIG_BYTES = 65_536
MAX_LOCAL_MEMORY_BYTES = 20_000
_HEADING_KIND_PATTERNS = (
    ("user_preference", re.compile(r"(?:偏好|习惯|preference|preferences)", re.IGNORECASE)),
    ("project_decision", re.compile(r"(?:决定|决策|架构决策|decision|decisions|adr)", re.IGNORECASE)),
    ("stable_fact", re.compile(r"(?:事实|环境|设备信息|fact|facts|environment)", re.IGNORECASE)),
    ("long_term_convention", re.compile(r"(?:约定|规范|惯例|convention|policy)", re.IGNORECASE)),
)


class LocalProviderError(ValueError):
    """端侧 Provider 的稳定错误。"""


@dataclass(frozen=True)
class LocalMemoryRecord:
    provider_id: str
    record_id: str
    source_revision: str
    title: str
    content: str
    kind: str
    scope: str
    occurred_at: str | None
    auto_share_eligible: bool
    blocked_reason: str | None = None

    def public_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = asdict(self)
        if not include_content or self.blocked_reason:
            result.pop("content", None)
        return result


@dataclass(frozen=True)
class ProviderPage:
    records: tuple[LocalMemoryRecord, ...]
    next_cursor: str | None


class LocalMemoryProvider(Protocol):
    provider_id: str
    provider_type: str
    display_name: str

    def list_records(self, *, cursor: str | None = None, limit: int = 50) -> ProviderPage: ...

    def get_records(self, record_ids: Iterable[str]) -> tuple[LocalMemoryRecord, ...]: ...


def _identifier(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", text):
        raise LocalProviderError(code)
    return text


def _bounded_limit(value: Any, *, maximum: int = 100) -> int:
    if isinstance(value, bool):
        raise LocalProviderError("LOCAL_PROVIDER_LIMIT_INVALID")
    try:
        return max(1, min(int(value or 50), maximum))
    except (TypeError, ValueError) as exc:
        raise LocalProviderError("LOCAL_PROVIDER_LIMIT_INVALID") from exc


def _kind_from_heading(heading: str) -> str:
    for kind, pattern in _HEADING_KIND_PATTERNS:
        if pattern.search(heading):
            return kind
    return "unclassified"


def _scope_for_kind(kind: str) -> str:
    return "user" if kind == "user_preference" else "workspace"


def _split_markdown(text: str) -> list[tuple[str, str]]:
    """按标题和段落拆分，并保留标题作为白名单分类依据。"""

    records: list[tuple[str, str]] = []
    heading = "未分类"
    buffer: list[str] = []

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if len(content) >= 8:
            records.append((heading, content))
        buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip() or "未分类"
            continue
        if not line:
            flush()
            continue
        buffer.append(line)
    flush()
    return records


class FileMemoryProvider:
    """从明确配置的 Markdown、JSON 或 JSONL 路径读取端侧记忆。"""

    provider_type = "files"

    def __init__(
        self,
        provider_id: str,
        display_name: str,
        paths: Iterable[str | Path],
        *,
        scanner: SensitiveContentScanner | None = None,
    ) -> None:
        self.provider_id = _identifier(provider_id, "LOCAL_PROVIDER_ID_INVALID")
        self.display_name = str(display_name or self.provider_id).strip()[:256]
        configured_paths = tuple(Path(path).expanduser().resolve() for path in paths)
        if not configured_paths:
            raise LocalProviderError("LOCAL_PROVIDER_PATH_REQUIRED")
        self._paths = configured_paths
        self._scanner = scanner or SensitiveContentScanner()

    def _source_files(self) -> tuple[Path, ...]:
        files: set[Path] = set()
        for configured in self._paths:
            if configured.is_file():
                if configured.suffix.lower() in SUPPORTED_FILE_SUFFIXES and not configured.is_symlink():
                    files.add(configured)
                continue
            if not configured.is_dir():
                continue
            for path in configured.rglob("*"):
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SUPPORTED_FILE_SUFFIXES:
                    continue
                resolved = path.resolve()
                try:
                    resolved.relative_to(configured)
                except ValueError:
                    continue
                files.add(resolved)
        return tuple(sorted(files, key=lambda item: str(item).casefold()))

    def _record_id(self, path: Path, ordinal: int) -> str:
        material = f"{self.provider_id}\x1f{path}\x1f{ordinal}".encode("utf-8")
        return f"local_{hashlib.sha256(material).hexdigest()[:32]}"

    def _record(
        self,
        *,
        path: Path,
        ordinal: int,
        title: str,
        content: str,
        kind: str,
        scope: str | None = None,
        occurred_at: str | None = None,
    ) -> LocalMemoryRecord:
        value = content.strip()
        if len(value.encode("utf-8")) > MAX_LOCAL_MEMORY_BYTES:
            blocked_reason = "LOCAL_MEMORY_TOO_LARGE"
        else:
            assessment = self._scanner.assess((value,))
            if assessment.has_sensitive_content:
                blocked_reason = "SENSITIVE_CONTENT"
            elif assessment.instruction_like:
                blocked_reason = "INSTRUCTION_LIKE_CONTENT"
            else:
                blocked_reason = None
        normalized_kind = kind if kind in AUTO_SHARE_KINDS else "unclassified"
        return LocalMemoryRecord(
            provider_id=self.provider_id,
            record_id=self._record_id(path, ordinal),
            source_revision=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            title=str(title or "未命名记忆").strip()[:256],
            content=value,
            kind=normalized_kind,
            scope=scope if scope in {"user", "workspace", "device", "agent", "private"} else _scope_for_kind(normalized_kind),
            occurred_at=str(occurred_at).strip()[:64] if occurred_at else None,
            auto_share_eligible=normalized_kind in AUTO_SHARE_KINDS and blocked_reason is None,
            blocked_reason=blocked_reason,
        )

    def _markdown_records(self, path: Path) -> list[LocalMemoryRecord]:
        text = path.read_text(encoding="utf-8", errors="replace")
        return [
            self._record(
                path=path,
                ordinal=ordinal,
                title=heading,
                content=content,
                kind=_kind_from_heading(heading),
            )
            for ordinal, (heading, content) in enumerate(_split_markdown(text), start=1)
        ]

    def _json_records(self, path: Path) -> list[LocalMemoryRecord]:
        text = path.read_text(encoding="utf-8", errors="strict")
        if path.suffix.lower() == ".jsonl":
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else [parsed]
        records: list[LocalMemoryRecord] = []
        for ordinal, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                continue
            content = str(value.get("content") or "").strip()
            if len(content) < 8:
                continue
            records.append(
                self._record(
                    path=path,
                    ordinal=ordinal,
                    title=str(value.get("title") or value.get("kind") or "结构化记忆"),
                    content=content,
                    kind=str(value.get("kind") or "unclassified"),
                    scope=str(value.get("scope") or ""),
                    occurred_at=str(value.get("occurred_at") or "") or None,
                )
            )
        return records

    def _all_records(self) -> tuple[LocalMemoryRecord, ...]:
        records: list[LocalMemoryRecord] = []
        for path in self._source_files():
            try:
                if path.suffix.lower() == ".md":
                    records.extend(self._markdown_records(path))
                else:
                    records.extend(self._json_records(path))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(records)

    def list_records(self, *, cursor: str | None = None, limit: int = 50) -> ProviderPage:
        bounded = _bounded_limit(limit)
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError as exc:
            raise LocalProviderError("LOCAL_PROVIDER_CURSOR_INVALID") from exc
        records = self._all_records()
        page = records[offset : offset + bounded]
        next_offset = offset + len(page)
        return ProviderPage(page, str(next_offset) if next_offset < len(records) else None)

    def get_records(self, record_ids: Iterable[str]) -> tuple[LocalMemoryRecord, ...]:
        requested = tuple(dict.fromkeys(_identifier(value, "LOCAL_RECORD_ID_INVALID") for value in record_ids))
        if not requested or len(requested) > 100:
            raise LocalProviderError("LOCAL_RECORD_SELECTION_INVALID")
        by_id = {record.record_id: record for record in self._all_records()}
        if any(record_id not in by_id for record_id in requested):
            raise LocalProviderError("LOCAL_RECORD_NOT_FOUND")
        return tuple(by_id[record_id] for record_id in requested)


class LocalProviderRegistry:
    def __init__(self, providers: Iterable[LocalMemoryProvider]) -> None:
        self._providers: dict[str, LocalMemoryProvider] = {}
        for provider in providers:
            if provider.provider_id in self._providers:
                raise LocalProviderError("LOCAL_PROVIDER_DUPLICATE")
            self._providers[provider.provider_id] = provider

    def list_sources(self) -> list[dict[str, str]]:
        return [
            {
                "provider_id": provider.provider_id,
                "provider_type": provider.provider_type,
                "display_name": provider.display_name,
            }
            for provider in sorted(self._providers.values(), key=lambda item: item.display_name.casefold())
        ]

    def require(self, provider_id: str) -> LocalMemoryProvider:
        normalized = _identifier(provider_id, "LOCAL_PROVIDER_ID_INVALID")
        try:
            return self._providers[normalized]
        except KeyError as exc:
            raise LocalProviderError("LOCAL_PROVIDER_NOT_FOUND") from exc


def load_provider_registry(config_path: str | Path | None = None) -> LocalProviderRegistry:
    configured = str(config_path or os.environ.get("MEMORY_LOCAL_PROVIDER_CONFIG") or "").strip()
    if not configured:
        return LocalProviderRegistry(())
    path = Path(configured).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_PROVIDER_CONFIG_BYTES:
        raise LocalProviderError("LOCAL_PROVIDER_CONFIG_INVALID")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise LocalProviderError("LOCAL_PROVIDER_CONFIG_INVALID") from exc
    provider_specs = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(provider_specs, list):
        raise LocalProviderError("LOCAL_PROVIDER_CONFIG_INVALID")
    providers: list[LocalMemoryProvider] = []
    for spec in provider_specs:
        if not isinstance(spec, dict):
            raise LocalProviderError("LOCAL_PROVIDER_CONFIG_INVALID")
        provider_type = str(spec.get("type") or "files").strip()
        if provider_type == "files":
            paths = spec.get("paths")
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise LocalProviderError("LOCAL_PROVIDER_PATH_REQUIRED")
            providers.append(
                FileMemoryProvider(
                    str(spec.get("id") or ""),
                    str(spec.get("display_name") or spec.get("id") or ""),
                    paths,
                )
            )
            continue
        loaded = False
        for entry_point in importlib.metadata.entry_points(group="agent_memory_gateway.memory_providers"):
            if entry_point.name == provider_type:
                providers.append(entry_point.load()(spec))
                loaded = True
                break
        if not loaded:
            raise LocalProviderError("LOCAL_PROVIDER_TYPE_UNSUPPORTED")
    return LocalProviderRegistry(providers)


class LocalMemoryShareService:
    """将用户在端侧明确选择的记录转换为现有不可变记忆事件。"""

    def __init__(self, registry: LocalProviderRegistry, client: Any) -> None:
        self.registry = registry
        self.client = client

    def list_sources(self) -> dict[str, Any]:
        return {"sources": self.registry.list_sources()}

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self.registry.require(str(payload.get("provider_id") or ""))
        page = provider.list_records(
            cursor=str(payload.get("cursor") or "") or None,
            limit=_bounded_limit(payload.get("limit")),
        )
        only_auto = bool(payload.get("only_auto_share_eligible"))
        records = tuple(record for record in page.records if not only_auto or record.auto_share_eligible)
        return {
            "provider_id": provider.provider_id,
            "records": [record.public_dict() for record in records],
            "next_cursor": page.next_cursor,
        }

    def share_selected(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self.registry.require(str(payload.get("provider_id") or ""))
        raw_ids = payload.get("record_ids")
        if not isinstance(raw_ids, list):
            raise LocalProviderError("LOCAL_RECORD_SELECTION_INVALID")
        workspace_id = _identifier(payload.get("workspace_id"), "WORKSPACE_ID_REQUIRED")
        records = provider.get_records(str(value) for value in raw_ids)
        return self._share_records(provider, records, workspace_id, capture_mode="manual_selection")

    def propose_eligible(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self.registry.require(str(payload.get("provider_id") or ""))
        workspace_id = _identifier(payload.get("workspace_id"), "WORKSPACE_ID_REQUIRED")
        page = provider.list_records(
            cursor=str(payload.get("cursor") or "") or None,
            limit=_bounded_limit(payload.get("limit"), maximum=25),
        )
        records = tuple(record for record in page.records if record.auto_share_eligible)
        result = self._share_records(provider, records, workspace_id, capture_mode="automatic_whitelist")
        result["next_cursor"] = page.next_cursor
        return result

    def _share_records(
        self,
        provider: LocalMemoryProvider,
        records: Iterable[LocalMemoryRecord],
        workspace_id: str,
        *,
        capture_mode: str,
    ) -> dict[str, Any]:
        results = []
        for record in records:
            if record.blocked_reason:
                results.append(
                    {
                        "record_id": record.record_id,
                        "status": "rejected",
                        "error": record.blocked_reason,
                    }
                )
                continue
            previous_event_id = self.client.outbox.provider_share_event(
                provider.provider_id,
                record.record_id,
                record.source_revision,
            )
            if previous_event_id:
                results.append(
                    {
                        "record_id": record.record_id,
                        "status": "already_shared",
                        "event_id": previous_event_id,
                    }
                )
                continue
            memory_payload = {
                "content": record.content,
                "kind": record.kind,
                "scope": record.scope,
                "workspace_id": workspace_id,
                "evidence": "user_explicit" if capture_mode == "manual_selection" else "agent_observed",
                "metadata": {
                    "provenance": {
                        "provider_type": provider.provider_type,
                        "provider_instance_id": provider.provider_id,
                        "source_record_id": record.record_id,
                        "source_revision": record.source_revision,
                        "capture_mode": capture_mode,
                    }
                },
            }
            result = self.client.remember(memory_payload)
            event_id = str(result.get("event_id") or "")
            if event_id and str(result.get("status") or "") not in {"rejected", "dead_letter"}:
                self.client.outbox.record_provider_share(
                    provider.provider_id,
                    record.record_id,
                    record.source_revision,
                    event_id,
                    capture_mode,
                )
            results.append({"record_id": record.record_id, **result})
        return {"provider_id": provider.provider_id, "results": results}
