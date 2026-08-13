"""旧记忆扫描、确认写入和按批次归档。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from .memory_app import load_sidecar_environment
from .security import SensitiveContentScanner
from .sidecar_daemon import LocalSidecarProxy, daemon_auth_token


MAX_IMPORT_RECORDS = 500
MAX_IMPORT_CONTENT_CHARS = 20_000
IMPORT_STATE_VERSION = 1
IMPORT_IDENTIFIER = re.compile(r"[A-Za-z0-9_.@:-]{1,128}\Z")
WORKSPACE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.@:-]{1,256}\Z")
SCANNER = SensitiveContentScanner()


class ImportWorkflowError(RuntimeError):
    """导入工作流的稳定错误码。"""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def infer_scope(path: Path, text: str) -> str:
    name = path.name.lower()
    joined = f"{path} {text}".lower()
    if name == "user.md":
        return "user"
    if name == "soul.md":
        return "agent"
    if "device" in joined or "端口" in text or "路径" in text:
        return "device"
    return "workspace"


def split_markdown(text: str) -> Iterable[str]:
    """将 Markdown 按标题、列表和段落切块。"""

    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                yield "\n".join(current).strip()
                current = []
            continue
        if stripped.startswith("#") or re.match(r"^[-*+]\s+", stripped):
            if current:
                yield "\n".join(current).strip()
            current = [stripped]
        else:
            current.append(stripped)
    if current:
        yield "\n".join(current).strip()


def _record_id(relative_path: str, chunk_hash: str) -> str:
    return hashlib.sha256(f"{relative_path}\0{chunk_hash}".encode("utf-8")).hexdigest()


def scan(source: Path, batch: str, output: Path) -> list[dict[str, Any]]:
    source = source.resolve()
    if not source.is_dir():
        raise ImportWorkflowError("IMPORT_SOURCE_DIRECTORY_REQUIRED")
    batch = str(batch).strip()
    if IMPORT_IDENTIFIER.fullmatch(batch) is None:
        raise ImportWorkflowError("IMPORT_BATCH_INVALID")
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有导入预览：{output}")

    records: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*.md"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ImportWorkflowError(f"IMPORT_SOURCE_ENCODING_INVALID:{path.name}") from exc
        relative_path = path.relative_to(source).as_posix()
        for chunk in split_markdown(text):
            if len(chunk) < 8:
                continue
            chunk_hash = content_hash(chunk)
            assessment = SCANNER.assess((chunk,))
            too_large = len(chunk) > MAX_IMPORT_CONTENT_CHARS
            status = (
                "blocked_sensitive"
                if assessment.has_sensitive_content
                else "blocked_instruction_like"
                if assessment.instruction_like
                else "blocked_too_large"
                if too_large
                else "imported_candidate"
            )
            records.append(
                {
                    "import_batch_id": batch,
                    "record_id": _record_id(relative_path, chunk_hash),
                    "source_path": relative_path,
                    "original_content_hash": chunk_hash,
                    **({"content": chunk} if status == "imported_candidate" else {}),
                    "scope": infer_scope(path, chunk),
                    "status": status,
                }
            )
            if len(records) > MAX_IMPORT_RECORDS:
                raise ImportWorkflowError("IMPORT_BATCH_TOO_LARGE")

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return records


def load_preview(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ImportWorkflowError("IMPORT_PREVIEW_UNREADABLE") from exc
    if len(lines) > MAX_IMPORT_RECORDS:
        raise ImportWorkflowError("IMPORT_BATCH_TOO_LARGE")
    records: list[dict[str, Any]] = []
    batches: set[str] = set()
    record_ids: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ImportWorkflowError("IMPORT_PREVIEW_INVALID") from exc
        if not isinstance(record, dict):
            raise ImportWorkflowError("IMPORT_PREVIEW_INVALID")
        batch = str(record.get("import_batch_id") or "").strip()
        record_id = str(record.get("record_id") or "").strip()
        content = record.get("content")
        scope = str(record.get("scope") or "")
        status = str(record.get("status") or "")
        content_digest = str(record.get("original_content_hash") or "")
        source_path = str(record.get("source_path") or "")
        if (
            not batch
            or re.fullmatch(r"[0-9a-f]{64}", record_id) is None
            or status not in {
                "imported_candidate",
                "blocked_sensitive",
                "blocked_instruction_like",
                "blocked_too_large",
            }
            or scope not in {"user", "workspace", "device", "agent", "private"}
            or re.fullmatch(r"[0-9a-f]{64}", content_digest) is None
            or not source_path
            or "\\" in source_path
            or source_path.startswith("/")
            or ".." in Path(source_path).parts
            or record_id != _record_id(source_path, content_digest)
            or (status == "imported_candidate" and not isinstance(content, str))
            or (isinstance(content, str) and content_hash(content) != content_digest)
        ):
            raise ImportWorkflowError("IMPORT_PREVIEW_INVALID")
        if record_id in record_ids:
            raise ImportWorkflowError("IMPORT_PREVIEW_DUPLICATE_RECORD")
        batches.add(batch)
        record_ids.add(record_id)
        records.append(record)
    if not records or len(batches) != 1:
        raise ImportWorkflowError("IMPORT_PREVIEW_INVALID")
    return records


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ImportWorkflowError("IMPORT_STATE_INVALID") from exc
    if not isinstance(state, dict) or state.get("version") != IMPORT_STATE_VERSION:
        raise ImportWorkflowError("IMPORT_STATE_INVALID")
    batch = str(state.get("import_batch_id") or "").strip()
    workspace_id = str(state.get("workspace_id") or "").strip()
    preview_digest = str(state.get("preview_sha256") or "")
    records = state.get("records")
    if (
        IMPORT_IDENTIFIER.fullmatch(batch) is None
        or WORKSPACE_IDENTIFIER.fullmatch(workspace_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", preview_digest) is None
        or not isinstance(records, list)
        or len(records) > MAX_IMPORT_RECORDS
    ):
        raise ImportWorkflowError("IMPORT_STATE_INVALID")
    provider_id = _provider_instance_id(batch)
    record_ids: set[str] = set()
    for entry in records:
        if not isinstance(entry, dict):
            raise ImportWorkflowError("IMPORT_STATE_INVALID")
        record_id = str(entry.get("record_id") or "")
        event_id = str(entry.get("event_id") or "")
        if (
            re.fullmatch(r"[0-9a-f]{64}", record_id) is None
            or record_id in record_ids
            or event_id != _event_id(workspace_id, provider_id, record_id)
        ):
            raise ImportWorkflowError("IMPORT_STATE_INVALID")
        record_ids.add(record_id)
    return state


def _provider_instance_id(batch: str) -> str:
    digest = hashlib.sha256(batch.encode("utf-8")).hexdigest()[:24]
    return f"memory-import-{digest}"


def _event_id(workspace_id: str, provider_id: str, record_id: str) -> str:
    return "evt_import_" + hashlib.sha256(
        f"{workspace_id}:{provider_id}:{record_id}".encode("utf-8")
    ).hexdigest()[:48]


def apply_preview(
    preview_path: Path,
    state_path: Path,
    workspace_id: str,
    client: Any,
    *,
    confirmed_by_user: bool,
    resume: bool = False,
) -> dict[str, Any]:
    if not confirmed_by_user:
        raise ImportWorkflowError("IMPORT_CONFIRMATION_REQUIRED")
    workspace_id = str(workspace_id).strip()
    if WORKSPACE_IDENTIFIER.fullmatch(workspace_id) is None:
        raise ImportWorkflowError("WORKSPACE_ID_INVALID")
    records = load_preview(preview_path)
    preview_digest = content_hash(preview_path.read_text(encoding="utf-8"))
    batch = str(records[0]["import_batch_id"])

    if state_path.exists():
        if not resume:
            raise ImportWorkflowError("IMPORT_STATE_EXISTS")
        state = _load_state(state_path)
        if state.get("preview_sha256") != preview_digest or state.get("workspace_id") != workspace_id:
            raise ImportWorkflowError("IMPORT_RESUME_MISMATCH")
    else:
        state = {
            "version": IMPORT_STATE_VERSION,
            "import_batch_id": batch,
            "preview_sha256": preview_digest,
            "workspace_id": workspace_id,
            "records": [],
        }
        _atomic_write_json(state_path, state)

    existing = {str(item.get("record_id")): item for item in state.get("records", [])}
    provider_id = _provider_instance_id(batch)
    for record in records:
        if record["status"] != "imported_candidate" or record["record_id"] in existing:
            continue
        event_id = _event_id(workspace_id, provider_id, record["record_id"])
        result = client.remember(
            {
                "event_id": event_id,
                "content": record["content"],
                "scope": record["scope"],
                "kind": "imported_note",
                "workspace_id": workspace_id,
                "evidence": "user_explicit",
                "confirmed_by_user": True,
                "metadata": {
                    "provenance": {
                        "provider_type": "memory-import",
                        "provider_instance_id": provider_id,
                        "source_record_id": record["record_id"],
                        "source_revision": record["original_content_hash"],
                        "capture_mode": "manual_selection",
                    },
                    "import_batch_id": batch,
                },
            }
        )
        entry = {
            "record_id": record["record_id"],
            "event_id": event_id,
            "status": str(result.get("status") or "unknown"),
            **({"backend_ref": str(result["backend_ref"])} if result.get("backend_ref") else {}),
            **({"error": str(result["error"])} if result.get("error") else {}),
        }
        state["records"].append(entry)
        existing[record["record_id"]] = entry
        _atomic_write_json(state_path, state)

    sync_result = client.sync(workspace_id)
    receipts = {
        str(item.get("event_id")): item
        for item in sync_result.get("receipts", [])
        if isinstance(item, dict)
    }
    for entry in state["records"]:
        receipt = receipts.get(str(entry.get("event_id")))
        if receipt:
            entry["status"] = str(receipt.get("status") or entry["status"])
            if receipt.get("backend_ref"):
                entry["backend_ref"] = str(receipt["backend_ref"])
            if receipt.get("error"):
                entry["error"] = str(receipt["error"])
    local_receipts = client.event_receipts(
        {"event_ids": [str(entry["event_id"]) for entry in state["records"]]}
    )
    by_event = {
        str(item.get("event_id")): item
        for item in local_receipts.get("receipts", [])
        if isinstance(item, dict)
    }
    for entry in state["records"]:
        receipt = by_event.get(str(entry["event_id"]))
        if not receipt:
            continue
        entry["status"] = str(receipt.get("status") or entry["status"])
        if receipt.get("backend_ref"):
            entry["backend_ref"] = str(receipt["backend_ref"])
        if receipt.get("result"):
            entry["result"] = str(receipt["result"])
        if receipt.get("error"):
            entry["error"] = str(receipt["error"])
    _atomic_write_json(state_path, state)
    rejected = sum(1 for item in state["records"] if item.get("error"))
    review_pending = sum(
        1 for item in state["records"] if item.get("result") == "candidate_created"
    )
    pending = sum(
        1
        for item in state["records"]
        if not item.get("backend_ref")
        and not item.get("error")
        and item.get("result") not in {"candidate_created", "source_duplicate"}
    )
    return {
        "status": (
            "completed_with_errors"
            if rejected
            else "review_pending"
            if review_pending
            else "sync_pending"
            if pending
            else "completed"
        ),
        "import_batch_id": batch,
        "submitted": len(state["records"]),
        "blocked": sum(1 for record in records if record["status"] != "imported_candidate"),
        "rejected": rejected,
        "pending": pending,
        "review_pending": review_pending,
        "state_file": str(state_path),
    }


def rollback_batch(state_path: Path, client: Any, *, confirmed_by_user: bool) -> dict[str, Any]:
    if not confirmed_by_user:
        raise ImportWorkflowError("IMPORT_ROLLBACK_CONFIRMATION_REQUIRED")
    state = _load_state(state_path)
    workspace_id = str(state.get("workspace_id") or "")
    client.sync(workspace_id)
    response = client.event_receipts(
        {"event_ids": [str(entry.get("event_id") or "") for entry in state.get("records", [])]}
    )
    refs_by_event = {
        str(item.get("event_id")): str(item.get("backend_ref"))
        for item in response.get("receipts", [])
        if isinstance(item, dict) and item.get("event_id") and item.get("backend_ref")
    }
    archived = 0
    pending: list[str] = []
    failed: list[dict[str, str]] = []
    for entry in state.get("records", []):
        if entry.get("rollback_status") == "archived":
            continue
        event_id = str(entry.get("event_id") or "")
        backend_ref = str(refs_by_event.get(event_id) or "")
        if not backend_ref:
            pending.append(event_id)
            continue
        result = client.forget(
            {
                "workspace_id": workspace_id,
                "memory_id": backend_ref,
                "hard_delete": False,
            }
        )
        entry["backend_ref"] = backend_ref
        entry["rollback_status"] = str(result.get("status") or "unknown")
        if entry["rollback_status"] == "archived":
            archived += 1
        else:
            failed.append({"event_id": event_id, "status": entry["rollback_status"]})
        _atomic_write_json(state_path, state)
    return {
        "status": "rolled_back" if not pending and not failed else "rollback_pending",
        "import_batch_id": state.get("import_batch_id"),
        "archived": archived,
        "pending_event_ids": pending,
        "failed": failed,
        "state_file": str(state_path),
    }


def _default_sidecar_key_file() -> Path:
    configured = os.environ.get("MEMORY_SIDECAR_KEY_FILE")
    if configured:
        return Path(configured)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "memory-gateway" / "secrets" / "pc-sidecar.env"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "memory-gateway" / "sidecar.env"


def sidecar_proxy(key_file: Path, port: int, agent_installation_id: str) -> LocalSidecarProxy:
    if not 1024 <= port <= 65535:
        raise ImportWorkflowError("SIDECAR_PORT_INVALID")
    values = load_sidecar_environment(key_file, require_private_permissions=os.name != "nt")
    return LocalSidecarProxy(
        f"http://127.0.0.1:{port}",
        daemon_auth_token(values["MEMORY_OUTBOX_KEY"]),
        agent_installation_id,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="扫描、确认导入和回滚本地旧记忆")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_cmd = sub.add_parser("scan", help="生成本地 JSONL 预览，不写共享库")
    scan_cmd.add_argument("--source", required=True, type=Path)
    scan_cmd.add_argument("--batch", required=True)
    scan_cmd.add_argument("--output", type=Path)

    def add_sidecar_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--sidecar-key-file", type=Path, default=_default_sidecar_key_file())
        command.add_argument("--sidecar-port", type=int, default=8766)
        command.add_argument("--agent-installation-id", required=True)

    apply_cmd = sub.add_parser("apply", help="将已人工检查的预览写入共享库")
    apply_cmd.add_argument("--preview", required=True, type=Path)
    apply_cmd.add_argument("--workspace-id", required=True)
    apply_cmd.add_argument("--state", type=Path)
    apply_cmd.add_argument("--confirmed-by-user", action="store_true")
    apply_cmd.add_argument("--resume", action="store_true")
    add_sidecar_arguments(apply_cmd)

    rollback_cmd = sub.add_parser("rollback", help="按本地批次状态归档已经写入的记忆")
    rollback_cmd.add_argument("--state", required=True, type=Path)
    rollback_cmd.add_argument("--confirmed-by-user", action="store_true")
    add_sidecar_arguments(rollback_cmd)
    args = parser.parse_args(argv)

    try:
        if args.command == "scan":
            output = args.output or Path(f"import-preview-{args.batch}.jsonl")
            records = scan(args.source, args.batch, output)
            print(json.dumps({"status": "scanned", "records": len(records), "preview_file": str(output)}, ensure_ascii=False))
            return
        proxy = sidecar_proxy(args.sidecar_key_file, args.sidecar_port, args.agent_installation_id)
        if not proxy.health():
            raise ImportWorkflowError("LOCAL_SIDECAR_UNAVAILABLE")
        if args.command == "apply":
            state = args.state or args.preview.with_suffix(args.preview.suffix + ".state.json")
            result = apply_preview(
                args.preview,
                state,
                args.workspace_id,
                proxy,
                confirmed_by_user=args.confirmed_by_user,
                resume=args.resume,
            )
        else:
            result = rollback_batch(args.state, proxy, confirmed_by_user=args.confirmed_by_user)
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] in {"completed_with_errors", "rollback_pending"}:
            raise SystemExit(2)
    except (ImportWorkflowError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
