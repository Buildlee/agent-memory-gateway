# 部署说明

本文只写通用流程，不包含现场信息。示例中的域名、路径、设备 ID、数据库连接串和密钥必须替换为你的本地配置；不要把真实值提交到 Git。

---

## 先选一条路径

- 只是想确认共享记忆的读写效果 — 先跑[快速上手](quickstart.md)的本地演示。只启动本机 SQLite Gateway，不改现有服务。
- 已有 Gateway，只让一台电脑的 Agent 接入 — 直接看下面的"在每台客户端设备上运行 Sidecar"。
- 准备上线新服务 — 从环境检查、迁移到容器启动依次完成。

---

## 开始前确认

- Python ≥ 3.10。
- 生产环境准备 PostgreSQL、容器运行时和 HTTPS 反向代理。
- Gateway、Worker、数据库和 Agent 客户端都用最小权限账号或受限网络。
- 准备独立的密钥：事件加密、令牌签名、刷新重放保护、敏感信息指纹和 Sidecar outbox 不能复用同一个值。
- 先备份既有数据库和运行配置，再执行任何迁移。

仓库只保留 `deploy/fn/.env.example` 这类变量示例。复制后形成的环境文件、主体配置、证书和私钥放到受保护的本机目录，已被 `.gitignore` 排除。

管理页需要放在 Gateway 所在的中枢环境时，按[中枢管理页](central-admin.md)配置独立的管理 Sidecar 和一次性浏览器会话；不要把本机回环管理页直接暴露到局域网。

---

## 在部署机自检

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src
```

测试通过只说明代码基线可用，不代表生产配置正确。

---

## 用安装向导发布

准备好密钥、证书、数据库备份和受保护的环境文件后，可以用一个入口完成发布。第一次先不带 `-Apply` 运行，只核对参数并显示将要使用的 SSH 主机、端口和 Gateway 名称：

```powershell
.\\scripts\\setup-shared-memory.ps1 `
  -Mode server `
  -SshHost "deploy-user@server" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -SecretsFile "/srv/memory-gateway/secrets.env" `
  -AdminEnvironmentFile "/srv/memory-gateway/admin/admin.env" `
  -GatewayPublicName "memory-gateway.internal" `
  -GatewayBindAddress "192.0.2.10"
```

进入维护窗口、确认备份和迁移状态后，在同一条命令末尾加 `-Apply`。这时才会创建发布目录、上传公开代码、构建镜像并启动 `memory-app` 与 HTTPS 代理。`memory-app` 在一个容器内监管 Gateway、Worker、管理 Sidecar 和管理页；Caddy 是唯一公开入口。

脚本不会替换受保护的环境文件，不生成或打印密钥，也不执行数据库迁移。

---

## 先迁移，再启动

生产 Gateway 使用独立的元数据库。先执行只读检查：

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --check
```

确认版本、扩展、权限和备份正确后，再执行：

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --apply
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --verify
```

如果接入可选的 PostgreSQL 长期记忆后端，也对它做检查和迁移：

```powershell
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --check
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --apply
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --verify
```

迁移命令不要接到 Gateway 启动脚本中。运行账号不应拥有建库、建角色或 schema 管理权限。

---

## 选择容器布局

默认布局只有两个容器。需要把管理进程与 Gateway 分开时，再选择高隔离布局。

| 文件 | 用途 |
|---|---|
| `deploy/fn/compose.slim.yaml` | **默认布局**：`memory-app` + Caddy，共两个容器 |
| `deploy/fn/compose.yaml` | **高隔离核心**：Gateway + Worker + Caddy |
| `deploy/fn/admin-console.compose.yaml` | **高隔离管理端**：独立管理 Sidecar + Web 管理页 |
| `deploy/fn/memory-mcp-bridge.compose.yaml` | **容器 Bridge**：让 Docker 内的 Agent 通过同网络命名空间接入共享记忆（独立部署） |

### 启动默认双容器服务

```powershell
docker compose --env-file "<发布环境文件>" --env-file "<管理环境文件>" -f deploy/fn/compose.slim.yaml config
docker compose --env-file "<发布环境文件>" --env-file "<管理环境文件>" -f deploy/fn/compose.slim.yaml up -d --build
```

先执行 `config`，确认密钥、数据库端口和卷映射没有暴露到公共网络。Gateway 和 Worker 使用同一版本镜像，只由代理暴露 HTTPS 入口；数据库不映射到宿主机公开端口。

### 使用高隔离布局

管理控制台依赖核心服务已运行。按[中枢管理页](central-admin.md)配置后，在中枢环境上使用双 Compose 文件启动：

```powershell
docker compose --env-file ".env" -f deploy/fn/compose.yaml -f deploy/fn/admin-console.compose.yaml up -d admin-sidecar admin-console
```

从高隔离布局切到默认布局时，旧服务会成为 Compose orphan。发布脚本不会自动删除或停止它们，也不使用 `--remove-orphans`；先核对新服务和数据，再经用户确认处理旧容器。

### 容器内 Agent 接入

Docker 内的 Agent（如 NAS Hermes）使用通用 Bridge 模板，与目标容器共用网络命名空间。详见[容器内 Agent 的统一接入](container-sidecar.md)。

---

## 每台客户端设备运行 Sidecar

每台设备只启动一个 Sidecar。推荐用安装向导完成一次性配对、独立运行环境、计划任务和 MCP 配置生成：

```powershell
.\\scripts\\setup-shared-memory.ps1 `
  -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" `
  -DefaultWorkspace "shared-workspace" `
  -Agent "codex-desktop|codex|Codex Desktop" `
  -InstallAutostart
```

管理员先提供一次性配对码。向导在隐藏输入中读取它，配对成功后把刷新凭据存入 Windows Credential Manager。生成的 MCP JSON 文件位于 `%LOCALAPPDATA%\memory-gateway\mcp`；导入对应客户端后重启 Agent。需要自定义目录、手动运行 Sidecar 或接入非 Windows 环境时，仍可用 `start-sidecar.ps1`、`install-sidecar-autostart.ps1` 和[示例说明](../examples/README.md)。

Sidecar 只监听本机回环地址。局域网内直连内部 HTTPS 地址；外网通过 VPN、零信任网络或受控隧道回到同一网络边界。无论什么网络路径，都不要关 TLS 证书校验。

升级 Gateway 时，让 Gateway、Worker 和每台 Sidecar 使用兼容版本。先完成服务端升级和健康检查，再在维护窗口逐台重启 Sidecar；每台重启后立刻做只读健康检查。不要用浏览器、脚本或数据库直连绕过旧 Sidecar。

Windows 计划任务从发布副本启动 Sidecar 时，优先使用该副本的 `src` 目录。先把已验证的发布副本放到任务工作目录，再在维护窗口重启 Sidecar；不要在 MCP 客户端运行时强制替换 `.exe` 启动文件。发布副本没有源码目录时，启动脚本回退到已安装包。

### deploy-fn-release.ps1 说明

`scripts/deploy-fn-release.ps1` 默认使用 `slim` 布局，并要求 `-AdminEnvironmentFile` 指向远端受保护的管理环境文件。传入 `-DeploymentProfile split` 可使用高隔离布局。脚本默认使用 SSH 22 端口；服务器改用其他端口时，显式传入 `-SshPort <端口>`（范围 1–65535）。

脚本默认发布自身所在仓库。需要从独立发布副本构建时，传入 `-ProjectRoot <已验证目录>`；该目录必须包含 `pyproject.toml`、`README.md`、`src`、`schema` 和 `deploy`。这样本地有未提交改动时，可以明确只发布已合并版本。

### DefaultWorkspace

`DefaultWorkspace` 不是示例名称，是正式登记过的工作区 ID。MCP 配置启动时也要传入同一个值；工具没有指定工作区时会直接报错，不猜测也不改用别的工作区。

Codex、Hermes 和 OpenClaw 的配置文件与字段说明见 [examples/README.md](../examples/README.md)。MCP 配置中只保留脚本路径和 Agent 安装实例 ID，密钥由本机受保护存储和 Sidecar 管理。

### 容器内的 Agent

Docker 内的 Agent 不需要专用版本。运行[容器内 Agent 的统一接入](container-sidecar.md)中的 `-Mode container`，安装器从目标容器的 Compose 标签识别项目，启动通用的 `memory-mcp-bridge`。Bridge 与目标容器共用网络命名空间，只提供 `http://127.0.0.1:8767/mcp`，不绑定宿主机端口。应用自身的 MCP 设置仍通过官方界面或 API 完成，不要改应用数据库。

---

## 上线前核对

按顺序检查：

1. `memory-app` 和代理健康检查通过；再分别核对 Gateway readiness、Worker 心跳、管理 Sidecar 与 `/admin/`。
2. 数据库迁移 `verify` 通过，运行账号符合最小权限。
3. 已登记设备能取得短期令牌，未登记设备被拒绝。
4. 一个 Agent 能写入、搜索和获取上下文；另一个已授权 Agent 能看到同一作用域结果。
5. 断开客户端网络后，事件进入加密 outbox；恢复后只同步一次。
6. 提交冲突候选并完成一次审核、撤销或归档，确认审计记录存在。
7. 检查日志、MCP 配置、Compose 配置和 Git 暂存区，确认没有真实密钥、令牌、连接串、证书或本机路径。

---

## 升级或恢复顺序

- 升级前记录当前镜像版本和迁移版本，备份数据库与受保护配置。
- 先在副本或维护窗口执行 `check`，再执行新增迁移和 `verify`。
- 失败时不要反复重放写入脚本；先检查事件账本、固定回执、死信和后端引用，再由 Worker 对账。
- 凭据轮换顺序：生成新值、更新消费者、验证、撤销旧值。不要直接覆盖仍在使用的密钥。

现场操作记录保存在忽略文件中，不应随代码上传。
