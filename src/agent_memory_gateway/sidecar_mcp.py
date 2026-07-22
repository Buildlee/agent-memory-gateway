"""MCP Sidecar 入口。"""

from __future__ import annotations

import argparse
import json
import os
import uuid

from .sidecar_daemon import get_shared_sidecar


def _active_workspace_id(workspace_id: str | None) -> str:
    """解析 MCP 请求的工作区，缺省时使用 Sidecar 的明确配置。"""

    selected = str(workspace_id or os.environ.get("MEMORY_DEFAULT_WORKSPACE") or "").strip()
    if not selected:
        raise ValueError("WORKSPACE_ID_REQUIRED")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="共享记忆 MCP Sidecar 桥接")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 MCP SDK，请运行：pip install -e \".[mcp]\"") from exc

    network_transport = args.transport == "streamable-http"
    mcp = FastMCP(
        "shared-memory",
        host=args.host,
        port=args.port,
        stateless_http=network_transport,
        json_response=network_transport,
        instructions=(
            "处理涉及项目、设备、用户偏好或既有决定的任务前，先调用 memory_context。"
            "任务完成后，只把有长期价值的内容写入共享记忆；端侧自动提议仅使用白名单类别。"
            "完整会话、临时过程、凭据和命令式内容不得进入共享记忆。"
        ),
    )
    client = get_shared_sidecar()

    @mcp.tool()
    def memory_local_sources() -> str:
        """列出本机已配置的个性化记忆来源；不读取或上传记忆正文。"""

        return json.dumps(client.local_sources({}), ensure_ascii=False, indent=2)

    @mcp.tool()
    def memory_local_preview(
        provider_id: str,
        cursor: str = "",
        limit: int = 50,
        only_auto_share_eligible: bool = False,
    ) -> str:
        """在本机预览可选择的个性化记忆；未选择内容不会发送到 Gateway。"""

        return json.dumps(
            client.local_preview(
                {
                    "provider_id": provider_id,
                    "cursor": cursor,
                    "limit": limit,
                    "only_auto_share_eligible": only_auto_share_eligible,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_share_selected(
        provider_id: str,
        record_ids: list[str],
        workspace_id: str | None = None,
        confirmed_by_user: bool = False,
    ) -> str:
        """共享用户已在端侧明确选择的记忆；未确认时不执行。"""

        if not confirmed_by_user:
            return json.dumps(
                {"status": "confirmation_required", "shared": 0}, ensure_ascii=False
            )
        return json.dumps(
            client.local_share_selected(
                {
                    "provider_id": provider_id,
                    "record_ids": record_ids,
                    "workspace_id": _active_workspace_id(workspace_id),
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_propose_local_candidates(
        provider_id: str,
        workspace_id: str | None = None,
        cursor: str = "",
        limit: int = 20,
    ) -> str:
        """把白名单类别的端侧记忆自动提议为待审核候选；不自动确认。"""

        return json.dumps(
            client.local_propose_eligible(
                {
                    "provider_id": provider_id,
                    "workspace_id": _active_workspace_id(workspace_id),
                    "cursor": cursor,
                    "limit": limit,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_context(
        query: str,
        workspace_id: str | None = None,
        max_items: int = 8,
        max_tokens: int = 1200,
    ) -> str:
        """获取当前任务可注入的共享记忆上下文。"""

        result = client.context(
            {
                "query": query,
                "workspace_id": _active_workspace_id(workspace_id),
                "max_items": max_items,
                "max_tokens": max_tokens,
            }
        )
        return result.get("context_pack", json.dumps(result, ensure_ascii=False))

    @mcp.tool()
    def memory_remember(
        content: str,
        scope: str = "workspace",
        kind: str = "note",
        workspace_id: str | None = None,
        confirmed_by_user: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """写入一条记忆事件；可选语义键用于后续冲突审核。"""

        payload = {
            "content": content,
            "scope": scope,
            "kind": kind,
            "workspace_id": _active_workspace_id(workspace_id),
        }
        if metadata:
            payload["metadata"] = metadata
        if confirmed_by_user:
            payload["evidence"] = "user_explicit"
        result = client.remember(payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def memory_search(query: str, workspace_id: str | None = None, limit: int = 8) -> str:
        """搜索共享记忆。"""

        result = client.search(
            {
                "query": query,
                "workspace_id": _active_workspace_id(workspace_id),
                "limit": limit,
            }
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def memory_feedback(
        memory_id: str,
        action: str,
        workspace_id: str | None = None,
        recall_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        """反馈一条记忆是否有用、过期、错误或需要置顶。"""

        return json.dumps(
            client.feedback(
                {
                    "memory_id": memory_id,
                    "action": action,
                    "workspace_id": _active_workspace_id(workspace_id),
                    "recall_id": recall_id,
                    "idempotency_key": idempotency_key or f"mcp_feedback_{uuid.uuid4().hex}",
                }
            ),
            ensure_ascii=False,
        )

    @mcp.tool()
    def memory_forget(memory_id: str, hard_delete: bool = False) -> str:
        """归档或删除一条记忆。"""

        return json.dumps(client.forget({"memory_id": memory_id, "hard_delete": hard_delete}), ensure_ascii=False)

    @mcp.tool()
    def memory_sync_status(workspace_id: str | None = None) -> str:
        """同步本地 outbox，并返回队列状态。"""

        return json.dumps(
            client.sync(workspace_id=_active_workspace_id(workspace_id)), ensure_ascii=False
        )

    @mcp.tool()
    def memory_cleanup_confirmed(confirmed_by_user: bool = False) -> str:
        """仅在用户已明确同意删除已确认的本机队列后，清理其加密副本。"""

        return json.dumps(client.cleanup_confirmed(confirmed_by_user=confirmed_by_user), ensure_ascii=False)

    @mcp.tool()
    def memory_list_reviews(workspace_id: str | None = None, limit: int = 30) -> str:
        """列出当前用户待审核记忆。仅在用户要求查看审核列表时调用。"""

        return json.dumps(
            client.list_reviews(
                {"workspace_id": _active_workspace_id(workspace_id), "limit": limit}
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_resolve_review(
        review_id: str,
        expected_revision: int,
        action: str,
        workspace_id: str | None = None,
        target_ref: str = "",
        edited_content: str = "",
        approve_instruction_like: bool = False,
        idempotency_key: str = "",
    ) -> str:
        """执行用户已明确选择的审核动作；不会删除记忆。"""

        payload: dict[str, object] = {
            "review_id": review_id,
            "expected_revision": expected_revision,
            "action": action,
            "workspace_id": _active_workspace_id(workspace_id),
            "idempotency_key": idempotency_key or f"mcp_review_{uuid.uuid4().hex}",
            "approve_instruction_like": approve_instruction_like,
        }
        if target_ref:
            payload["target_ref"] = target_ref
        if edited_content:
            payload["content"] = edited_content
        return json.dumps(client.resolve_review(payload), ensure_ascii=False, indent=2)

    @mcp.tool()
    def memory_revert_review(
        review_id: str,
        operation_id: str,
        expected_revision: int,
        workspace_id: str | None = None,
        idempotency_key: str = "",
    ) -> str:
        """撤销最近一次错误审核，创建补偿记录并保留历史。"""

        return json.dumps(
            client.revert_review(
                {
                    "review_id": review_id,
                    "operation_id": operation_id,
                    "expected_revision": expected_revision,
                    "workspace_id": _active_workspace_id(workspace_id),
                    "idempotency_key": idempotency_key or f"mcp_revert_{uuid.uuid4().hex}",
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_rebuild_crystal(
        scope: str,
        namespace_key: str,
        workspace_id: str | None = None,
        idempotency_key: str = "",
    ) -> str:
        """重算指定授权范围的结晶页；只在用户要求更新汇总记忆时调用。"""

        return json.dumps(
            client.rebuild_crystal(
                {
                    "scope": scope,
                    "namespace_key": namespace_key,
                    "workspace_id": _active_workspace_id(workspace_id),
                    "idempotency_key": idempotency_key or f"mcp_crystal_{uuid.uuid4().hex}",
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_admin_overview(workspace_id: str | None = None) -> str:
        """查看当前工作区的审核、重试、死信和设备概览。只读。"""

        return json.dumps(
            client.admin_overview({"workspace_id": _active_workspace_id(workspace_id)}),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_admin_devices(workspace_id: str | None = None) -> str:
        """列出当前工作区内已绑定的设备和 Agent。只读，不返回凭据。"""

        return json.dumps(
            client.list_admin_devices({"workspace_id": _active_workspace_id(workspace_id)}),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_admin_audit(workspace_id: str | None = None, limit: int = 50) -> str:
        """查看当前工作区的近期审计记录。只读，不返回记忆正文。"""

        return json.dumps(
            client.list_admin_audit(
                {"workspace_id": _active_workspace_id(workspace_id), "limit": limit}
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_admin_dead_letters(workspace_id: str | None = None, limit: int = 50) -> str:
        """列出当前工作区未处理的死信。只读，不返回记忆正文。"""

        return json.dumps(
            client.list_admin_dead_letters(
                {"workspace_id": _active_workspace_id(workspace_id), "limit": limit}
            ),
            ensure_ascii=False,
            indent=2,
        )

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
