"""MCP Sidecar 入口。"""

from __future__ import annotations

import json

from .sidecar_client import SidecarClient


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 MCP SDK，请运行：pip install -e \".[mcp]\"") from exc

    mcp = FastMCP("shared-memory")
    client = SidecarClient()

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
    ) -> str:
        """写入一条记忆事件；离线时会进入本地 outbox。"""

        result = client.remember(
            {
                "content": content,
                "scope": scope,
                "kind": kind,
                "workspace_id": workspace_id,
            }
        )
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
    def memory_sync_status() -> str:
        """同步本地 outbox，并返回队列状态。"""

        return json.dumps(client.sync(), ensure_ascii=False)

    mcp.run()


if __name__ == "__main__":
    main()
