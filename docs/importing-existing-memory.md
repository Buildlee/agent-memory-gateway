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

每条材料保留来源路径、内容哈希和批次号。审核记录标明来源、确认者和后续替代关系，便于定位并撤销对应批次。

---

## 使用 memory-import scan 生成预览

`memory-import` 是直接可用的 CLI 命令。`scan` 子命令只读取指定目录并输出 JSONL 预览，不会写入共享库：

```bash
memory-import scan --source ./memory-folder --batch import_2026_07_03
```

`scan` 的行为：
- 递归扫描 `--source` 下所有 `.md` 文件
- 按标题、列表和段落将 Markdown 分块（`split_markdown`）
- 每块通过 `SensitiveContentScanner` 做敏感信息检测
- 敏感内容记录标记为 `blocked_sensitive`，其余标记为 `imported_candidate`
- 推断作用域（`infer_scope`）：`user.md` → `user`，`soul.md` → `agent`，含设备特征 → `device`，其余 → `workspace`
- 输出 JSONL，每条包含 `import_batch_id`、`source_path`、`original_content_hash`、`content`、`scope`、`status`

预览文件默认写入 `import-preview-{batch}.jsonl`，可通过 `--output` 指定路径。文件留在本机受保护目录，检查后再进入审核。

---

## 敏感信息扫描规则

扫描器（`SensitiveContentScanner`，位于 `security.py`）匹配以下类别：

- **私钥**：PEM 格式（RSA/OpenSSH/EC/DSA/PGP）
- **API 令牌**：`sk-*`、`ghp_*`、`github_pat_*`、Slack `xox*`、Stripe `sk_live_*`、Firebase `AIza*`
- **云凭证**：AWS Access Key（`AKIA`/`ASIA`）
- **Bearer Token**：Authorization 头中的 bearer 值
- **会话令牌**：JWT（`eyJ*.*.*`）、Cookie 头
- **数据库连接串**：`postgres://user:pass@host`、`mysql://`、`mongodb+srv://`、`redis://`
- **凭据赋值**：`api_key = xxx`、`password = xxx` 等模式，排除占位符和代码表达式
- **助记词**：seed phrase / mnemonic / recovery code
- **支付卡号**：Luhn 校验通过的 13-19 位数字
- **中国身份证号**：18 位带校验码

扫描结果不泄漏原文内容，只返回类别、位置和可选 HMAC 指纹。

---

## 审核流程

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

批量导入的写入、回滚和结晶重建会围绕预览和审核流程提供工具，避免旧资料未经检查就影响协作。
