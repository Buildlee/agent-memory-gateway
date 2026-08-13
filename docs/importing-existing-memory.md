# 导入已有记忆

`MEMORY.md`、`USER.md`、项目笔记和本机记录可以加速共享库的初始化。来源可信度不同，导入时先当待审核材料处理。

---

## 导入前整理材料

分开保存来源，避免把完整会话、临时日志和密码文件送进扫描器。优先处理已确认的偏好、项目决定、设备事实和长期有效的工作约定。

| 来源 | 常见归属 | 注意点 |
|---|---|---|
| `USER.md` | `user` | 只保留用户确认且长期有效的偏好 |
| `MEMORY.md` | `workspace` 或 `agent` | 区分项目共识与 Agent 私有经验 |
| `SOUL.md` | `agent` 或 `private` | 不把角色设定自动扩大为工作区知识 |
| 本机路径、端口、硬件记录 | `device` | 只让需要这台设备的 Agent 读取 |
| 项目架构决定 | `workspace` | 附上来源和确认时间，便于追溯 |
| 旧任务状态 | `session` 或 `archived` | 过期状态先保留为历史，不当当前事实 |

---

## 处理流程

```text
本地资料 → scan(JSONL 预览) → 敏感信息扫描 → 分类/分块/作用域判断 → 去重与冲突检查 → 人工审核 → 写入共享库
```

预览在本机保留相对来源路径、内容哈希和批次号。正式写入只提交稳定的来源记录 ID、内容哈希和批次号，不上传本机路径。批次状态文件记录事件 ID 和服务端引用，用于恢复和回滚。

---

## 使用 memory-import scan 生成预览

`memory-import` 是直接可用的 CLI 命令。`scan` 子命令只读取指定目录并输出 JSONL 预览，不会写入共享库：

```powershell
memory-import scan --source ./memory-folder --batch import_2026_07_03
```

`scan` 的行为：
- 递归扫描 `--source` 下所有 `.md` 文件
- 按标题、列表和段落将 Markdown 分块（`split_markdown`）
- 每块通过 `SensitiveContentScanner` 做敏感信息检测
- 敏感内容标记为 `blocked_sensitive`，命令式内容标记为 `blocked_instruction_like`，超长内容标记为 `blocked_too_large`，其余才是 `imported_candidate`
- 推断作用域（`infer_scope`）：`user.md` → `user`，`soul.md` → `agent`，含设备特征 → `device`，其余 → `workspace`
- 输出 JSONL，每条包含 `import_batch_id`、`source_path`、`original_content_hash`、`content`、`scope`、`status`

预览文件默认写入 `import-preview-{batch}.jsonl`，可通过 `--output` 指定路径。文件留在本机受保护目录，检查后再确认写入。

## 确认写入与恢复

先人工检查 JSONL，只保留允许进入目标工作区的候选项。Sidecar 运行后执行：

```powershell
memory-import apply `
  --preview .\import-preview-import_2026_07_03.jsonl `
  --workspace-id shared-workspace `
  --agent-installation-id codex-desktop `
  --confirmed-by-user
```

`apply` 只处理 `imported_candidate`，并通过本机回环 Sidecar 走正常授权、敏感检查、outbox 和同步协议。它不会直连数据库。状态默认写入预览文件旁的 `.state.json`；中断后以完全相同的预览和工作区加 `--resume`，事件 ID 保持稳定，不会重复导入。结果为 `sync_pending` 时先恢复网络后重试；`review_pending` 表示记录已进入正常审核队列；只有取得长期记忆引用且无错误时才返回 `completed`。

如果 Sidecar key 或端口不是默认值，显式传入 `--sidecar-key-file` 和 `--sidecar-port`。导入 Agent 必须具有目标工作区的写入和同步能力。

## 回滚批次

回滚不是硬删除，而是通过正常 `memory.forget` 流程归档该批次已经落库的记忆：

```powershell
memory-import rollback `
  --state .\import-preview-import_2026_07_03.jsonl.state.json `
  --agent-installation-id codex-desktop `
  --confirmed-by-user
```

已经归档的条目可重复执行而不重复处理；尚未取得服务端引用的事件会列在 `pending_event_ids`，归档请求未成功的条目会列在 `failed`。先恢复同步或处理对应错误，再次运行回滚。回滚只采用 Sidecar 本机终态回执里的服务端引用，不信任状态文件中可编辑的引用字段。保留状态文件，直到批次验收或回滚完成。

---

## 敏感信息扫描规则

扫描器（`SensitiveContentScanner`，位于 `security.py`）匹配以下类别：

- **私钥**：PEM 格式（RSA/OpenSSH/EC/DSA/PGP）
- **API 令牌**：`sk-*`、`ghp_*`、`github_pat_*`、Slack `xox*`、Stripe `sk_live_*`、Firebase `AIza*`
- **云凭证**：AWS Access Key（`AKIA`/`ASIA`）
- **Bearer Token**：Authorization 头中的 bearer 值
- **会话令牌**：JWT（`eyJ*.*.*`）、Cookie 头
- **凭据赋值**：`api_key = xxx`、`password = xxx` 等模式，排除占位符和代码表达式
- **助记词**：seed phrase / mnemonic / recovery code
- **支付卡号**：Luhn 校验通过的 13-19 位数字
- **中国身份证号**：18 位带校验码

扫描结果不泄漏原文内容，只返回类别、位置和可选 HMAC 指纹。

---

## 后续审核流程

导入的候选记忆通过 `PostgresReviewService`（`review_service.py`）进入审核：

- **list_pending**：列出待审核的候选记忆，含冲突检测结果
- **resolve**：执行审核操作——`confirm`、`confirm_edit`、`retain_both`、`supersede`、`archive`、`reject`
- **revert**：撤销已执行的审核操作

审核中的核查要点：

- 内容是否已由用户、项目文档或可信来源确认
- 作用域是否足够小：设备信息不应变成所有工作区可读
- 是否与现有事实冲突（冲突时保留双方来源，交由人工处理）
- 是否包含可识别的凭据、私密路径或命令式内容
- 是否需要设置有效期、归档状态或后续复核时间

导入证据标记为用户明确确认；与现有事实冲突时，仍可进入正常审核和替代流程。结晶重建继续使用管理审核接口，不由导入器自动触发。

## 持续共享端侧记忆

`memory-import` 适合一次性迁移历史资料。来源会持续变化时，给 Sidecar 配置端侧 Provider：先用 `memory_local_preview` 预览，再通过 `memory_share_selected` 人工选择，或用 `memory_propose_local_candidates` 提议四类白名单内容。Provider 不上传本机路径，也不会把中枢结果写回原系统。
