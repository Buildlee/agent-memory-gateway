# OpenClaw HTTP 接入示例

如果 OpenClaw 侧已有本地 gateway 或 routing 层，可以直接调用 HTTP API。

写入记忆事件：

```http
POST http://127.0.0.1:8787/v1/events
Content-Type: application/json

{
  "content": "OpenClaw 当前 workspace 使用本地 gateway routing。",
  "scope": "workspace",
  "kind": "fact",
  "agent_id": "openclaw",
  "device_id": "desktop-4090",
  "workspace_id": "OpenClaw"
}
```

获取上下文：

```http
POST http://127.0.0.1:8787/v1/context
Content-Type: application/json

{
  "query": "当前 workspace 有哪些共享记忆？",
  "agent_id": "openclaw",
  "device_id": "desktop-4090",
  "workspace_id": "OpenClaw"
}
```
