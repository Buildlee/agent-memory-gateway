# Agent Memory Gateway

<p align="center">
  <a href="README.md">中文</a>
  ·
  <a href="README_EN.md">English</a>
</p>

<p align="center">
  <strong>让 Codex、Hermes 和未来接入的 Agent 共享真正可用的长期记忆。</strong><br>
  有来源、有权限、可离线、能反馈，也能接住各端原有的个性化记忆。
</p>

<p align="center">
  <a href="#三分钟体验"><img src="https://img.shields.io/badge/本地体验-无需_API_key-2ea44f" alt="无需 API key"></a>
  <a href="#接入方式"><img src="https://img.shields.io/badge/MCP-Codex%20%7C%20Hermes%20%7C%20OpenClaw-5a67d8" alt="支持 MCP"></a>
  <a href="https://github.com/Buildlee/agent-memory-gateway/actions/workflows/validate.yml"><img src="https://github.com/Buildlee/agent-memory-gateway/actions/workflows/validate.yml/badge.svg" alt="自动验证"></a>
  <a href="#许可证"><img src="https://img.shields.io/badge/license-MIT-4b5563" alt="MIT"></a>
  <a href="#三分钟体验"><img src="https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python" alt="Python 3.10+"></a>
</p>

不同 Agent 各自保存记忆时，信息很快会散落在设备、工作区和各自的记忆插件里。Agent Memory Gateway 在它们之间增加一层受控的共享记忆：每台设备只运行一个本机 Sidecar，中枢负责身份、权限、去重、审核、召回和审计。现有 Markdown、JSON、JSONL 或第三方记忆系统不必被替换，可以通过 Provider 按需提议到共享库。

它带来的变化不是“多存一份资料”，而是让 Agent 在回答前先召回跨端积累，并把有用、过时或错误的结果反馈给后续排序。每次召回都有 `recall_id`，管理页能看到哪些设备和 Agent 真正使用了共享记忆，同时不会展示原始查询或端侧文件路径。

## 🚀 快速上手

### 三分钟体验

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
cd agent-memory-gateway
.\scripts\setup-local-demo.ps1
```

做完会看到 `status: ready` 且 `cross_agent_results > 0`。两条演示 Agent（`demo-codex`、`demo-hermes`）在同一个工作区完成了读写验证。Gateway 在后台继续跑，结束时停掉对应进程：

```powershell
Stop-Process -Id <脚本输出的 process_id>
```

演示数据保留在 `%LOCALAPPDATA%\agent-memory-gateway-demo`，不涉及设备和加密同步。默认端口被占用时指定 `-Port` 和 `-DemoHome` 即可。

### 一条命令接入正式服务

客户端支持 Windows、Linux 和 macOS。安装器会引导填写 Gateway、工作区和需要接入的 Agent，再隐藏读取一次性配对码。Windows 使用当前用户计划任务，Linux 使用 systemd 用户服务，macOS 使用 LaunchAgent；三种平台使用同一份非敏感配置、配对协议和 MCP 工具。

管理员先生成一份不含凭据的安装配置，再把它放到受控文件共享、设备管理工具或内部 HTTPS 地址。客户端只需运行一条命令，然后输入一次隐藏的配对码：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Buildlee/agent-memory-gateway/main/scripts/memory-device-install.ps1)))
```

Linux / macOS：

```bash
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/Buildlee/agent-memory-gateway/main/scripts/memory-device-install.sh | sh
```

安装器会生成稳定设备 ID，自动识别 Codex、Hermes 和 OpenClaw，创建独立运行环境和登录后自启的 Sidecar，并按客户端格式生成 MCP 配置。它只在 Sidecar 启动且首个 Agent 完成一次 Gateway 同步后才报告 `ready`。最后按输出路径把 `shared-memory` 合并到对应 Agent 的 MCP 配置并重启 Agent；安装器不会擅自改写第三方客户端配置。配对码、刷新凭据、私钥和数据库地址不会进入安装配置、命令行或 MCP JSON。

安装后的日常维护统一使用：

```powershell
memory-device status
memory-device doctor
memory-device repair          # 默认只显示修复计划
memory-device upgrade --package <已校验的源码目录或 wheel> --release-id v0.2.0
memory-device rollback        # 默认只显示回滚计划
memory-device uninstall       # 默认只显示卸载计划并保留凭据与本地记忆
```

确认安全修复时加 `--apply`。确认卸载时加 `--yes`；只有明确加上 `--purge-credentials` 或 `--purge-data` 才删除设备身份或本地队列。卸载前会备份运行配置和 MCP 配置。独立运行环境和 `memory-device` 维护命令会保留，方便诊断或重新接入；它们不再自启，也不能访问已卸载的 Sidecar 配置。

```powershell
memory-gateway pairing-code `
  --tenant-id personal --user-id user-a --device-type windows --agent-types codex,hermes,openclaw `
  --workspace-id agent-memory-gateway `
  --capabilities memory.feedback,memory.forget,memory.read_context,memory.search,memory.sync,memory.write_event
```

配对码只交给待接入设备并在过期前使用。没有带工作区授权的旧配对码仍可使用，但配对后需要管理员手动绑定。

如果新电脑只拿到安装脚本，安装器默认读取 GitHub 最新稳定 Release 的清单，并在源码包 SHA-256 完全一致后安装。项目尚未发布稳定版本时，交互向导会询问是否临时使用开发版 `main`，未确认或非交互环境都会停止，不会静默降级；开发测试也可显式传入 `-Channel development`，Linux/macOS 则设置 `MEMORY_DEVICE_CHANNEL=development`。安装配置中的固定发布包仍具有最高优先级。

需要手动指定 Agent、设备 ID 或恢复中断安装时，仍可使用底层完整向导：

管理员生成一次性配对码后，在客户端运行：

```powershell
.\scripts\setup-shared-memory.ps1 -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" -DefaultWorkspace "shared-workspace" `
  -Agent @("codex-desktop|codex|Codex Desktop", "hermes-desktop|hermes|Hermes Desktop", "openclaw-desktop|openclaw|OpenClaw") `
  -InstallAutostart
```

向导完成设备配对、密钥和凭据写入、启动只监听 `127.0.0.1` 的 Sidecar，最后生成 MCP 配置。服务端用 `-Mode server`（加 `-Apply` 执行）。默认是 `memory-app + Caddy` 两个容器；已有的拆分部署仍可作为高隔离模式使用。详见[部署说明](docs/deployment.md)。

Linux 或 macOS 在已安装项目后运行同一套 Python CLI：

```powershell
memory-device install --profile ./device-install.json
```

安装器隐藏读取配对码，刷新凭据写入仅当前账号可读的文件，Sidecar 分别注册为 systemd 用户服务或 LaunchAgent。中断后使用相同参数加 `--resume`；已有配置不一致时会停止，不会覆盖。

## 🔧 系统结构

```mermaid
flowchart LR
  A["Codex / Hermes / OpenClaw"] -->|MCP 或本机 HTTP| S["Memory Sidecar<br/>(127.0.0.1 only)"]
  S -->|HTTPS| P["Caddy<br/>唯一公开入口"]
  P --> APP["memory-app<br/>Gateway · Worker · Admin"]
  APP --> M[("生产元数据与审计<br/>PostgreSQL")]
  APP --> B[("生产长期记忆<br/>GBrain / PostgreSQL 适配器")]
  L["端侧记忆<br/>Markdown / JSON / 插件"] -->|"人工选择或白名单提议"| S
  R["中枢管理页"] -->|"HTTPS /admin"| P
```

| 层 | 组件 | 职责 |
|----|------|------|
| 接入层 | Codex / Hermes / OpenClaw | MCP 或 HTTP 请求记忆 |
| 本机层 | Memory Sidecar | 凭据、加密 outbox、缓存，不暴露到局域网 |
| 服务层 | Memory Gateway | 身份验证、权限判断、事件账本、查询和审核 |
| 存储层 | PostgreSQL + GBrain/适配器 | 生产审计、授权和可检索记忆；SQLite 仅供本地演示 |

默认 `slim` 布局把 Gateway、Worker、管理 Sidecar 和管理页放在同一应用容器，但每个子进程只继承自身所需的敏感环境变量。这是运维精简布局，不等同于容器级隔离；需要更强故障域和文件系统隔离时使用 `split` 布局。

正式环境的管理页部署在 Gateway 所在中枢，通过固定 HTTPS `/admin/` 地址访问。浏览器首次完成一次性授权后，会话在有效期内保持；日常可以直接打开该地址。除了审核、设备和运行状态，页面还会展示各 Agent 的实际召回、用户反馈和端侧来源汇总。设备权限变更与撤销都经过确认、版本校验和审计。

## 📦 CLI 命令

| 命令 | 作用 | 示例 |
|------|------|------|
| `memory-gateway` | 启动 HTTP Gateway | `memory-gateway --host 127.0.0.1 --port 8787` |
| `memory-app` | 启动默认一体化服务 | `memory-app` |
| `memory-sidecar-mcp` | MCP Sidecar 桥接 | `memory-sidecar-mcp --transport streamable-http --port 8767` |
| `memory-sidecar-daemon` | 本机 Sidecar 守护进程 | `MEMORY_DEVICE_RUNTIME_CONFIG` 配好后运行 `memory-sidecar-daemon` |
| `memory-device` | 设备安装、诊断、修复、升级、回滚和卸载 | `memory-device onboard`、`memory-device doctor` |
| `memory-import` | 导入既有记忆 | `memory-import scan --source ./notes --batch 2026_07` |
| `memory-admin-check` | 管理健康检查 | `memory-admin-check` |
| `memory-admin-console` | 启动管理 Web 页 | `memory-admin-console --port 18700` |

```powershell
pip install -e ".[mcp,postgres]"
memory-gateway --help
```

## 🧩 核心模块

### 服务层

| 模块 | 文件 | 职责 |
|------|------|------|
| HTTP 服务 | `gateway.py` | 健康检查、事件读写、同步 push/pull、审核、管理页 |
| 身份与权限 | `auth.py` | O(1) token hash 认证 + 工作区/能力集权限判断 |
| 加密 Outbox | `outbox.py` | 离线写入加密入列，恢复后按序同步 |
| 同步协议 | `sync_service.py` | push/pull：事件回执、游标增量、墓碑标记 |
| 限流 | `rate_limit.py` | 认证入口滑动窗口限流 |
| 数据库连接池 | `db_pool.py` | PostgreSQL 连接池，支持忙碌回退 |
| 迁移工具 | `migrate.py`, `schema.py` | 数据库版本迁移和 schema 管理 |

### 存储与检索

| 模块 | 文件 | 职责 |
|------|------|------|
| SQLite 存储 | `store.py` | 共享记忆的 SQLite 实现 |
| 元数据账本 | `metadata_store.py` | 事件审计、工作区授权、设备注册 |
| 查询服务 | `query_service.py` | 授权过滤后检索 |
| 反馈服务 | `feedback_service.py` | 记录召回反馈并提供有界排序信号 |
| 混合检索 | `hybrid_retrieval.py` | 关键词 + CJK n-gram + 去重 + 预算裁剪 + MMR 多样性 |
| 评分衰减 | `scoring.py` | 记忆按半衰期衰减（preference 180d / fact 90d / temporary 3d） |
| 向量索引 | `gbrain_backend.py`, `gbrain.py` | 长期记忆的向量检索后端 |
| 结晶记忆 | `crystal_service.py` | 稳定记忆整理为结晶页面，可审计重建 |

### 安全与凭据

| 模块 | 文件 | 职责 |
|------|------|------|
| 安全扫描 | `security.py` | 识别密码/私钥/令牌/连接串，标记命令式内容 |
| 加密 | `crypto.py` | outbox 和同步内容的 AES-GCM 加密 |
| 凭据管理 | `file_credential.py`, `windows_credential.py` | 文件或 Windows Credential Manager 读写 |
| 设备配对 | `device_pair.py`, `device_key.py` | 一次性配对码、设备密钥生成与验证 |

### Sidecar 与管理

| 模块 | 文件 | 职责 |
|------|------|------|
| MCP Sidecar | `sidecar_mcp.py` | 暴露 `memory_context`/`memory_remember`/`memory_sync_status` |
| 端侧 Provider | `local_provider.py` | 读取本机记忆并安全提议到共享库 |
| 本机 Daemon | `sidecar_daemon.py` | 单实例，多 Agent 通过回环 RPC 共用 |
| 审核服务 | `review_service.py` | 待审核观察与审批工作流 |
| 管理控制台 | `admin_console.py`, `admin_check.py` | 本机备用入口、中枢 Web 管理页和健康检查 |
| 导入工具 | `importer.py` | 把既有资料导入共享库 |

### 一条记忆的处理流程

```
写入 → 敏感检查(security.py) → 幂等去重(metadata_store.py) → 确认/审核
  → 授权过滤检索(query_service.py + hybrid_retrieval.py) → recall_id → 反馈/遗忘/归档/撤销
```

稳定记忆可整理为结晶页（`crystal_service.py`），来源变化后需显式重建。

## 🔒 安全边界

- Agent 配置不保存 Gateway 刷新凭据、数据库连接串或私钥
- 请求体字段只能声明意图，不能自行扩大权限
- Gateway 在检索前过滤未授权记录，后端不承担权限判断
- 离线写入经过加密 outbox，同步后清理需用户确认
- 内网服务同样使用 HTTPS；外网通过 VPN / 零信任 / 受控隧道接入
- 示例和日志不包含真实令牌、证书、私钥、连接串或内网地址

## 🔌 接入方式

| 方式 | 场景 | 参考 |
|------|------|------|
| Codex MCP | 本机 Codex 共享项目/偏好 | [codex-mcp.json](examples/codex-mcp.json) |
| Hermes MCP | 同设备多 Agent 共用 | [hermes-mcp.json](examples/hermes-mcp.json) |
| OpenClaw MCP | 正式接入 | [openclaw-mcp.json](examples/openclaw-mcp.json) |
| OpenClaw HTTP | 本地原型或自定义工作流 | [openclaw-http.md](examples/openclaw-http.md) |
| 标准 MCP 客户端 | 支持 MCP 的 Agent | [示例说明](examples/README.md) |
| 容器内 Agent | Docker + Streamable HTTP MCP | [容器 Sidecar](docs/container-sidecar.md) |

## 📖 文档

- [快速上手](docs/quickstart.md) — 本地体验、正式接入、常见问题
- [总体设计](docs/design-v2.md) — 身份、权限、同步、审核和检索的实现边界
- [部署说明](docs/deployment.md) — PostgreSQL、HTTPS、迁移、上线核对
- [中枢管理页](docs/central-admin.md) — 在 Gateway 所在环境部署和打开 `/admin`
- [日常运维与恢复](docs/operations.md) — 管理页、运行检查、死信排查、恢复演练
- [开发与验证](docs/development.md) — 测试命令、检索口径、修改约定
- [导入已有记忆](docs/importing-existing-memory.md) — 把既有资料迁入共享库
- [容器 Agent 接入](docs/container-sidecar.md) — 通用 Docker Sidecar、MCP Bridge 与重建后对账

## 🔨 开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m pytest tests/ -v
```

提交前跑完整测试和编译检查，具体约定见[开发与验证](docs/development.md)。

```powershell
python -m unittest discover -s tests
python -m compileall -q src tests
git diff --check
```

## 🤝 参与贡献

欢迎提交可复现的问题和脱敏后的改进建议。涉及协议、权限、迁移或安全边界的改动请同步更新测试和文档。不要在 issue、提交信息、示例或日志中粘贴真实凭据。

## 📄 许可证

[MIT](LICENSE)
