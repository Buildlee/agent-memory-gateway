# 开发与验证

这份文档记录仓库里的开发约定。只使用通用路径和变量名；本机账号、服务器地址、证书、密钥和现场操作记录不应写进来。

## 本地开发基线

```powershell
python -m venv .venv
.\\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src tests
```

修改功能前先确认这两项通过。修改后至少运行受影响的模块测试，再跑完整测试集。

---

## CI 验证

GitHub Actions 在功能分支和 PR 上运行：完整测试、Python 编译、PowerShell 语法检查、敏感信息扫描、补丁格式检查。不读取环境文件、证书、数据库、Windows 凭据或现场运维脚本。

安全分类器专用语料包含故意无效的私钥、令牌和连接串，用来证明拦截规则有效。CI 字面扫描只排除这份固定测试语料；对应单元测试确认这些样例带有明确无效标记。

CI 通过说明公开代码可合并，不代表功能已部署到运行中的 Sidecar。需要管理页等新本机能力时，仍要在维护窗口更新设备并运行只读健康检查。

---

## 本地体验脚本

`scripts/setup-local-demo.ps1` 是第一次体验的入口。建立仓库内忽略的 `.local-demo-venv`，安装项目，调用 `scripts/start-local-demo.ps1` 启动只监听 `127.0.0.1` 的 SQLite Gateway。演示主体、令牌、数据库和日志保存在仓库外的 `DemoHome` 中。

脚本默认让两个模拟 Agent 完成一次交叉检索。修改演示脚本时用 `[System.Management.Automation.Language.Parser]::ParseFile` 检查语法错误，确认：

- `DemoHome` 位于仓库外，已有目录时拒绝覆盖。
- Gateway 只绑定 `127.0.0.1`，端口占用时拒绝启动。
- 随机令牌不写入终端、日志、仓库或示例文件。
- 第二个 Agent 能检索到第一个 Agent 写入的测试记录。

完整使用方式见[快速上手](quickstart.md)。

---

## Sidecar 部署

`start-sidecar.ps1`、`install-sidecar-autostart.ps1` 和 `start-sidecar-mcp.ps1` 都要求传入 `DefaultWorkspace`，必须是已登记的工作区 ID。MCP 调用没有 `workspace_id` 时使用此值；未配置时报错。

启动脚本在当前发布副本包含 `src` 时，通过 `PYTHONPATH` 加载该目录，不写入全局 Python 环境。这样避免 Windows 在 MCP 客户端长期运行时锁住 `.exe` 启动文件。修改启动路径时至少运行完整测试、PowerShell 语法检查和 `tests.test_release_safety`。

远程发布脚本的 `SshPort` 必须在 1-65535 之间，默认 22。SSH 命令和 SCP 上传使用同一端口。

发布前若本地仍在开发分支，用 `-ProjectRoot` 指向已验证的主分支发布副本。脚本会验证发布副本的必要文件后才开始远程操作。

迁移 SQL 在根目录 `schema/` 下维护，同时以只读副本随包发布到 `agent_memory_gateway/_schema`。容器和源码目录优先使用根目录副本；已安装包自动使用包内副本。改动 SQL 后必须同步两个位置并运行完整测试，避免本地安装、Windows Sidecar 和容器计算出不同校验值。

---

## 安装向导回归点

`scripts/setup-shared-memory.ps1` 是实际接入入口，不是演示脚本别名。修改它或设备配对客户端时至少运行：

```powershell
python -m unittest tests.test_device_pair tests.test_setup_installer tests.test_release_safety
python -m compileall -q src tests
```

向导必须守住以下行为：

- 配对码只通过 `Read-Host -AsSecureString` 从标准输入读取。
- 刷新凭据只写入 Windows Credential Manager（`write_generic_credential`）。
- MCP JSON 只包含命令和参数，不含 Gateway 令牌、刷新凭据或私钥。
- 已有本机 key、计划任务、运行环境和 MCP JSON 均拒绝覆盖。
- 配对完成后的恢复只允许 `-UseExistingCredential`，要求原设备私钥存在。
- 服务端模式没有 `-Apply` 时不连接远端，也不创建发布目录。
- 公开受信任的 HTTPS 地址不应因默认不存在的 CA 而失败；内部 CA 必须由用户明确传入并校验。

---

## 混合检索

检索代码在 `src/agent_memory_gateway/hybrid_retrieval.py`。接收已通过授权过滤的候选，不自行判断谁能读取记忆。

- 词匹配负责英文、数字和常见符号。
- 中文拆成单字和相邻双字，能找回包含中文词组的记录。
- 同一特征生成固定本地哈希向量，补足纯文本匹配。无网络请求，无第三方向量 API。
- 内容规范化后相同，或向量相似度 ≥ 0.94 的候选，只保留排序靠前的一条。
- MMR 重排：`0.80 * base_score - 0.20 * similarity + group_bonus`，避开放入与已选结果太像的内容，优先补进不同作用域或类型。

评分公式：`base_score = 0.50 * lexical + 0.35 * vector_score + 0.15 * confidence`（有查询时）；无查询时直接用置信度。

`PostgresQueryService` 先拿当前主体可见的 `backend_ref`，请求 GBrain 返回对应事实。结果中混入未授权事实时在服务端丢弃（`source is None → continue`），不会出现在响应中。

---

## 上下文预算

`memory_context` 的 `max_tokens` 只能在 64-12,000 之间，默认 1,200（常量定义于 `hybrid_retrieval.py`，sidecar_mcp.py 默认值 1200）。限制的是记忆引用的估算量，不含安全说明和 JSON 字段本身。

每条引用先估算内容，再加固定开销；已选引用的 `token_estimate` 不超过 `token_budget`。候选放不下时整条跳过，不截断正文。响应带回：

- `retrieval.candidate_count`：进入混合排序的候选数
- `retrieval.duplicate_count`：因重复排除的数量
- `retrieval.budget_skipped_count`：因预算不足未返回的数量
- `incomplete`：预算导致有候选未返回时为 `true`

预算值无效（超出范围或非数值）时返回稳定错误 `MAX_TOKENS_INVALID` / `MAX_TOKENS_OUT_OF_RANGE`，不静默放大。

---

## 离线模式

Sidecar 只从已授权的本机缓存和加密 outbox 取内容。使用同一套检索和预算逻辑，结果写入 `offline: true, incomplete: true`（`sidecar_client.py` 中的 `_offline_search` 和 `_offline_context`）。Gateway 返回认证或权限错误时，Sidecar 不退缓存；缓存只用于网络不可用或服务无响应的情况。

---

## 测试回归

```powershell
python -m unittest tests.test_hybrid_retrieval
python -m unittest tests.test_query_service tests.test_sidecar_sync
python -m unittest discover -s tests
python -m compileall -q src tests
```

覆盖中文匹配、文本归一化去重、排序稳定性、多样性、预算上限、未授权事实过滤、离线 Sidecar 行为。提交前运行 `git diff --check`，扫描本次改动是否带入真实域名、内网地址、账号、令牌、私钥或本机路径。

---

## 管理接口

管理端数据先由 Gateway 统一授权，再通过 Sidecar 取回。浏览器或 MCP 不能绕过这条路径直接读 PostgreSQL。

```powershell
python -m unittest tests.test_admin_service tests.test_gateway_admin
python -m unittest tests.test_sidecar_daemon tests.test_sidecar_mcp
python -m unittest tests.test_admin_check tests.test_admin_console
python -m compileall -q src tests
```

- 管理接口要求 `memory.manage`（`gateway.py` 中 `/v1/admin/*`、`/v1/reviews/*`、`/v1/crystals/rebuild` 均映射到此能力），无此权限返回 `CAPABILITY_FORBIDDEN`。
- 概览只返回数量和 worker 心跳；设备列表不返回公钥或凭据；审计列表不返回 `details_json` 和记忆正文；死信列表只返回稳定 ID、错误码、类别和时间。
- 每个查询按调用者租户、用户和工作区过滤。工作区缺失或未授权时，不退任何默认工作区。
- Sidecar RPC 只允许已声明管理方法，使用现有短期令牌和本机回环鉴权。
- `memory-admin-console` 只监听回环地址（`host="127.0.0.1"`，启动时校验）。首次 URL 含一次性 `session` 令牌，换取 HttpOnly Cookie 后即失效（`consume_launch_token` 标记已用）。页面源码、API 响应和测试断言不包含本机 key、Gateway 令牌或刷新凭据。
- 改变审核状态的请求必须带 `confirmed_by_user: true`、`expected_revision` 和 `idempotency_key`。

`memory-admin-check` 给计划任务或外部监控使用。从 Sidecar 获取概览，检测 worker 心跳、待重试事件和未处理死信，输出不含正文或凭据的 JSON：

- 退出码 `0`：状态正常
- 退出码 `1`：发现问题（心跳过期、重试事件、死信）
- 退出码 `2`：本机 Sidecar、配置或授权不可用

Windows 通过 `scripts/check-admin-health.ps1` 启动；脚本只从受保护的本机文件读取 Sidecar key，不写入输出。
