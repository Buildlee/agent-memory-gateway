# 多 Agent 共享记忆系统设计

## 核心架构

```text
Codex / Hermes Agent / OpenClaw / 其他 Agent
        |
        | MCP / HTTP
        v
本机 Memory Sidecar
        |
        v
Memory Gateway
        |
        +-- 记忆事件
        +-- 记忆条目
        +-- 作用域
        +-- 冲突关系
        +-- 遗忘曲线
        +-- 上下文包
```

## 作用域

| Scope | 用途 |
|---|---|
| `user` | 用户长期偏好 |
| `workspace` | 项目级事实、决策、约束 |
| `agent` | 某个 agent 的程序性经验 |
| `device` | 某台电脑的路径、端口、硬件、环境 |
| `session` | 当前会话短期状态 |
| `private` | 只给特定 agent 或设备使用 |
| `shared` | 明确允许跨 agent 共享的事实 |

## 冲突处理

不同设备上的端口、路径、硬件默认不冲突。比如：

```text
电脑 A：Hermes 使用 8748 端口
电脑 B：Hermes 使用 8648 端口
```

这属于不同 `device_id` 的设备事实。

真正冲突示例：

```text
旧记忆：用户希望使用 Obsidian 作为核心同步层
新记忆：用户决定不折腾 Obsidian
```

旧记忆应标记为 `superseded`，而不是删除。

## 遗忘曲线

召回分数：

```text
score =
  semantic_relevance
  * confidence
  * importance
  * freshness
  * reinforcement
  * scope_match
```

新鲜度：

```text
freshness = exp(-age_days / half_life_days)
```

默认半衰期：

| 类型 | 半衰期 |
|---|---:|
| 用户偏好 | 180 天 |
| 项目事实 | 90 天 |
| 任务状态 | 14 天 |
| 临时备注 | 3 天 |
| 程序性经验 | 365 天 |
| 设备事实 | 120 天 |

## 记忆结晶

多条重复或相近的记忆应被合并成更稳定的总结。

示例：

```text
用户喜欢中文输出
文档和注释默认中文
报告默认中文
```

结晶为：

```text
用户默认要求文档、报告、代码注释使用中文，除非特别说明。
```

结晶记忆必须保留来源事件，方便回溯和重新计算。

## 默认接入方式

所有 agent 默认只接一个 MCP server：

```json
{
  "mcp_servers": {
    "shared-memory": {
      "command": "memory-sidecar-mcp",
      "env": {
        "MEMORY_GATEWAY_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```
