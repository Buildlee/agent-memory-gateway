# 开发与验证

这份文档记录仓库里的开发约定。它只使用通用路径和变量名；本机账号、服务器地址、证书、密钥和现场操作记录不应写进来。

## 本地开始前先跑一遍基线

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src tests
```

修改功能前先确认这两项通过。修改后至少运行受影响模块的测试，再跑完整测试集。数据库迁移、容器部署和真实凭据验证属于单独步骤，不能用本地测试代替。

## 持续验证与发布边界

GitHub Actions 会在功能分支和 Pull Request 上运行完整测试、Python 编译、所有公开 PowerShell 脚本的语法检查、公开文件敏感信息扫描和补丁格式检查。它不读取环境文件、证书、数据库、Windows 凭据或现场运维脚本。

安全分类器的专用语料包含故意无效的私钥、令牌和连接串形状，用来证明拦截规则有效。CI 的字面扫描只排除这一份固定测试语料，其他公开文件仍会扫描；对应单元测试会确认这些样例带有明确的无效标记。

CI 通过说明公开代码可合并，不代表已把功能装到正在运行的 Sidecar。需要管理页等新本机能力时，仍要按部署说明在维护窗口更新对应设备，并运行实际只读健康检查。

## 本地体验脚本

`scripts/setup-local-demo.ps1` 是给第一次体验准备的入口。它会建立仓库内忽略的 `.local-demo-venv`，安装当前项目，然后调用 `scripts/start-local-demo.ps1` 启动只监听 `127.0.0.1` 的 SQLite Gateway。演示主体、随机令牌、数据库和日志都保存在仓库外的 `DemoHome` 中。

脚本默认会让两个模拟 Agent 完成一次交叉检索。修改演示脚本时，至少检查下面四项：

```powershell
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path .\scripts\start-local-demo.ps1),
  [ref]$tokens,
  [ref]$errors
)
$errors
```

- `DemoHome` 位于仓库外，且已有目录时拒绝覆盖。
- Gateway 只绑定 `127.0.0.1`，端口占用时拒绝启动。
- 随机令牌不会写入终端、日志、仓库或示例文件。
- 第二个演示 Agent 能检索到第一个 Agent 写入的无敏感信息测试记录。

完整使用方式见 [快速上手](quickstart.md)。

## Sidecar 的默认工作区

`start-sidecar.ps1`、`install-sidecar-autostart.ps1` 和 `start-sidecar-mcp.ps1` 都要求传入 `DefaultWorkspace`。它必须是已经登记的工作区 ID。MCP 调用没有写 `workspace_id` 时会使用这个值；没有配置就报错，不会把占位文本当成可访问的工作区。

这两个启动脚本在当前发布副本包含 `src` 时，会通过本次进程的 `PYTHONPATH` 加载该目录，不会写入全局 Python 环境。这样可以避免 Windows 在 MCP 客户端长期运行时锁住 `.exe` 启动文件，导致包升级中断。修改这条启动路径时，至少运行完整测试、PowerShell 语法检查和 `tests.test_release_safety`。

飞牛发布脚本的 `SshPort` 必须在 1 到 65535 之间，默认 22。修改远程发布逻辑时，需要同时检查 SSH 命令和 SCP 上传都使用同一个端口；不要把现场 SSH 地址、账号、端口映射或 secret 路径写进公开代码和文档。

发布前若本地仍在开发分支，使用 `-ProjectRoot` 指向一个已验证的主分支发布副本。不要靠临时切换当前工作目录来赌上传内容；脚本会验证发布副本的必要文件后才开始远程操作。

迁移 SQL 在仓库根目录的 `schema` 下维护，同时会以完全相同的只读副本随 Python 包发布到 `agent_memory_gateway/_schema`。容器和源码目录优先使用根目录副本；已安装包没有仓库目录时自动使用包内副本。改动任一 SQL 后必须同步这两个位置，并运行完整测试，避免本地安装、Windows Sidecar 和容器计算出不同的迁移校验值。

## 混合检索怎么工作

检索代码位于 `src/agent_memory_gateway/hybrid_retrieval.py`。它接收已经完成授权过滤的候选，而不是自己决定谁能读取记忆。

- 词匹配负责英文、数字和常见符号组成的关键词。
- 中文会拆成单字和相邻双字，因此“工作区权限”也能找回包含相邻中文词组的记录。
- 同一份特征会生成固定的本地哈希向量，用来补足纯文本匹配。这里没有网络请求，也没有第三方向量 API。
- 内容规范化后相同，或向量相似度很高的候选，只保留排序较靠前的一条。
- MMR 重排会避开和已选结果太像的内容，并优先补进不同作用域或类型的记录。

`PostgresQueryService` 会先拿到当前主体可见的 `backend_ref`，再请求 GBrain 返回这些引用对应的事实。结果中如果混入不在授权集合里的事实，会在服务端丢弃，不会出现在响应中。

## 上下文预算的口径

`memory_context` 的 `max_tokens` 只能在 64 到 12,000 之间，默认 1,200。这个数限制记忆引用的估算量，不限制固定安全说明和 JSON 字段本身。

每条引用先按内容做保守估算，再加固定开销；已选引用的 `token_estimate` 不会超过 `token_budget`。候选放不下时不会截断正文，而是整条跳过。响应会带上：

- `retrieval.candidate_count`：进入混合排序的候选数。
- `retrieval.duplicate_count`：因重复而排除的数量。
- `retrieval.budget_skipped_count`：因预算不足而未返回的数量。
- `incomplete`：预算导致有候选未返回时为 `true`。

预算值无效不会被悄悄改大，Gateway 会返回稳定错误。这样调用方可以明确调整请求，而不是误以为自己拿到了较小预算的结果。

## 网络断开时

Sidecar 只会从已经授权的本机缓存和加密 outbox 里取内容。它仍使用同一套检索和预算逻辑，并把 `offline: true`、`incomplete: true` 写进结果。Gateway 返回认证或权限错误时，Sidecar 不应退回缓存；缓存只用于网络暂时不可用或服务暂时无法响应的情况。

## 第七阶段需要守住的回归点

```powershell
python -m unittest tests.test_hybrid_retrieval
python -m unittest tests.test_query_service tests.test_sidecar_sync
python -m unittest discover -s tests
python -m compileall -q src tests
```

测试覆盖中文匹配、文本归一化去重、排序稳定性、多样性、预算上限、未授权事实过滤和离线 Sidecar 行为。提交前还要运行 `git diff --check`，并扫描本次改动是否带入真实域名、内网地址、账号、令牌、私钥或本机路径。

## 第八阶段：管理接口的回归点

管理端的数据先由 Gateway 统一授权，再通过现有 Sidecar 取回。浏览器或 MCP 不能绕过这条路径直接读 PostgreSQL。

```powershell
python -m unittest tests.test_admin_service tests.test_gateway_admin
python -m unittest tests.test_sidecar_daemon tests.test_sidecar_mcp
python -m unittest tests.test_admin_check tests.test_admin_console
python -m compileall -q src tests
```

- 管理接口都要求 `memory.manage`，没有该权限必须返回 `CAPABILITY_FORBIDDEN`。
- 概览只返回数量和 worker 心跳；设备列表不返回公钥或任何凭据；审计列表不返回 `details_json` 和记忆正文；死信列表只返回稳定 ID、错误码、类别和时间。
- 每个查询都按调用者的租户、用户和工作区过滤。工作区缺失或不在授权范围内时，不能退回任何默认工作区。
- Sidecar RPC 只允许已声明的管理方法，并继续使用现有短期令牌和本机回环鉴权。
- `memory-admin-console` 只能监听回环地址。首次 URL 里的 session token 只换取一次 HttpOnly Cookie；页面源码、API 响应和测试断言都不能包含本机 key、Gateway 令牌或刷新凭据。会改变审核状态的请求必须带 `confirmed_by_user=true`、revision 和幂等键。

`memory-admin-check` 是给计划任务或外部监控使用的只读命令。它从 Sidecar 获取概览，检测 worker 心跳、待重试事件和未处理死信，并输出不含正文或凭据的 JSON。状态正常时退出码为 `0`；发现运行问题时为 `1`；本机 Sidecar、配置或授权不可用时为 `2`。Windows 可通过 `scripts/check-admin-health.ps1` 启动它；脚本只从受保护的本机文件读取 Sidecar key，不会把 key 写入输出。
