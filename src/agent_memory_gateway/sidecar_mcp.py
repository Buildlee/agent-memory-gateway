"""MCP Sidecar 入口。"""

from __future__ import annotations

import json
import uuid

from .sidecar_daemon import get_shared_sidecar


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 MCP SDK，请运行：pip install -e \".[mcp]\"") from exc

    mcp = FastMCP("shared-memory")
    client = get_shared_sidecar()

    @mcp.tool()
    def memory_context(query: str, workspace_id: str = "default", max_items: int = 8) -> str:
        """获取当前任务可注入的共享记忆上下文。"""

        result = client.context({"query": query, "workspace_id": workspace_id, "max_items": max_items})
        return result.get("context_pack", json.dumps(result, ensure_ascii=False))

    @mcp.tool()
    def memory_remember(
        content: str,
        scope: str = "workspace",
        kind: str = "note",
        workspace_id: str = "default",
        confirmed_by_user: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """写入一条记忆事件；可选语义键用于后续冲突审核。"""

        payload = {
            "content": content,
            "scope": scope,
            "kind": kind,
            "workspace_id": workspace_id,
        }
        if metadata:
            payload["metadata"] = metadata
        if confirmed_by_user:
            payload["evidence"] = "user_explicit"
        result = client.remember(payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def memory_search(query: str, workspace_id: str = "default", limit: int = 8) -> str:
        """搜索共享记忆。"""

        result = client.search({"query": query, "workspace_id": workspace_id, "limit": limit})
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def memory_feedback(memory_id: str, action: str) -> str:
        """反馈一条记忆是否有用、过期、错误或需要置顶。"""

        return json.dumps(client.feedback({"memory_id": memory_id, "action": action}), ensure_ascii=False)

    @mcp.tool()
    def memory_forget(memory_id: str, hard_delete: bool = False) -> str:
        """归档或删除一条记忆。"""

        return json.dumps(client.forget({"memory_id": memory_id, "hard_delete": hard_delete}), ensure_ascii=False)

    @mcp.tool()
    def memory_sync_status(workspace_id: str = "default") -> str:
        """同步本地 outbox，并返回队列状态。"""

        return json.dumps(client.sync(workspace_id=workspace_id), ensure_ascii=False)

    @mcp.tool()
    def memory_cleanup_confirmed(confirmed_by_user: bool = False) -> str:
        """仅在用户已明确同意删除已确认的本机队列后，清理其加密副本。"""

        return json.dumps(client.cleanup_confirmed(confirmed_by_user=confirmed_by_user), ensure_ascii=False)

    @mcp.tool()
    def memory_list_reviews(workspace_id: str = "default", limit: int = 30) -> str:
        """列出当前用户待审核记忆。仅在用户要求查看审核列表时调用。"""

        return json.dumps(
            client.list_reviews({"workspace_id": workspace_id, "limit": limit}),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def memory_resolve_review(
        review_id: str,
        expected_revision: int,
        action: str,
        workspace_id: str = "default",
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
            "workspace_id": workspace_id,
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
        workspace_id: str = "default",
        idempotency_key: str = "",
    ) -> str:
        """撤销最近一次错误审核，创建补偿记录并保留历史。"""

        return json.dumps(
            client.revert_review(
                {
                    "review_id": review_id,
                    "operation_id": operation_id,
                    "expected_revision": expected_revision,
                    "workspace_id": workspace_id,
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
        workspace_id: str = "default",
        idempotency_key: str = "",
    ) -> str:
        """重算指定授权范围的结晶页；只在用户要求更新汇总记忆时调用。"""

        return json.dumps(
            client.rebuild_crystal(
                {
                    "scope": scope,
                    "namespace_key": namespace_key,
                    "workspace_id": workspace_id,
                    "idempotency_key": idempotency_key or f"mcp_crystal_{uuid.uuid4().hex}",
                }
            ),
            ensure_ascii=False,
            indent=2,
        )

    mcp.run()


if __name__ == "__main__":
    main()
