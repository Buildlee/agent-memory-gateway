# 快速上手

分两条路：先在本机跑一遍共享记忆体验，或者把已部署的 Gateway 接到真实 Agent。第一条路不需要账号、API key、容器或数据库；第二条路保留设备、工作区和凭证的安全边界。

---

## 先跑一次本地体验

需要 Python 3.10+。在 PowerShell 运行：

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
Set-Location agent-memory-gateway
.\scripts\setup-local-demo.ps1
```

第一次运行做三件事：

1. 在仓库下建 `.local-demo-venv`，装演示依赖。
2. 在 `%LOCALAPPDATA%\agent-memory-gateway-demo` 建临时主体配置、随机令牌和 SQLite 数据库。
3. 启动 Gateway（只监听 `127.0.0.1`），让两个模拟 Agent 完成一次写入和交叉检索。

终端输出一个对象。看这两个字段：

```text
status                : ready
cross_agent_results   : 1
```

`cross_agent_results` 大于 0 说明第二个 Agent 找到了第一个 Agent 写入的演示记忆。令牌不会打印到终端——只存在演示目录的本机文件里。脚本不碰已运行的 Codex、Hermes、Docker 或远程 Gateway。

### 停掉演示

Gateway 以后台进程继续跑。结束后，用脚本输出的 `process_id` 停掉它：

```powershell
Stop-Process -Id <process_id>
```

演示数据会保留，不自动删除。再次运行提示 `DemoHome 已存在` 时，指定一个新目录——避免覆盖令牌和数据库：

```powershell
.\scripts\setup-local-demo.ps1 `
  -DemoHome "$env:LOCALAPPDATA\agent-memory-gateway-demo-02" `
  -Port 18787
```

### 这次体验验证了什么

| 已验证 | 说明 |
|---|---|
| 共用工作区 | `demo-codex` 与 `demo-hermes` 都只能访问 `demo-workspace`。 |
| 身份匹配 | 两个 Agent 用不同随机令牌，Gateway 根据令牌哈希识别调用者。 |
| 写入与检索 | Agent1 写入一条事实，Agent2 通过搜索找回。 |
| 数据不出本机 | Gateway 只绑定 `127.0.0.1`；不调用第三方模型、向量 API 或远程数据库。 |

本地体验帮你理解工作方式。设备配对、短期令牌、加密 outbox、PostgreSQL 元数据和 HTTPS 部署属于正式服务，下一节说明怎么接入。

---

## 接入已部署的共享服务

管理员先生成不含凭据的安装配置，客户端使用一条命令接入。安装配置包含 Gateway 地址、工作区、稳定设备 ID 前缀和 Agent 模板；它不允许包含配对码、刷新凭据、私钥、令牌、密码或数据库连接串。

Windows 可直接使用系统 PowerShell 运行网页一键命令；Linux 与 macOS 需要 Python 3.10+。三种平台都安装独立运行环境，并只在回环地址启动 Sidecar。

管理员只需准备一次配置：

```powershell
.\scripts\new-device-install-profile.ps1 `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DefaultWorkspace "shared-workspace" `
  -OutputPath "C:\secure-share\device-install.json"
```

每台新设备都使用一枚独立配对码。生成配对码时同时附上该设备需要的最小工作区能力，Gateway 会在配对事务内把这些能力绑定给本次登记的 Agent；客户端不具备自行授权的能力：

```powershell
memory-gateway pairing-code `
  --tenant-id personal --user-id user-a --device-type windows --agent-types codex,hermes,openclaw `
  --workspace-id agent-memory-gateway `
  --capabilities memory.feedback,memory.forget,memory.read_context,memory.search,memory.sync,memory.write_event
```

输出的配对码只通过受控渠道交给对应设备，不写入安装配置、聊天记录或命令行历史。旧版本生成的配对码没有工作区信息，仍可用于兼容接入，但需要管理员在配对后手动绑定。

客户端只拿到安装脚本时，默认读取 GitHub 最新稳定 Release 清单，并校验固定源码包的 SHA-256。若需使用内部发布源，可制作不可变 ZIP 发布包并放到受控 HTTPS 地址。生成配置时提供发布包本地文件，脚本会自动计算并写入 SHA-256：

```powershell
.\scripts\new-device-install-profile.ps1 `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DefaultWorkspace "shared-workspace" `
  -ReleaseArchiveUrl "https://releases.example.internal/agent-memory-gateway-v0.1.0.zip" `
  -ReleaseArchivePath "C:\releases\agent-memory-gateway-v0.1.0.zip" `
  -ReleaseId "agent-memory-gateway-v0.1.0" `
  -OutputPath "C:\secure-share\device-install.json"
```

发布包地址必须是 HTTPS 且不可变；客户端仅在下载结果与配置中的 SHA-256 完全一致时解压运行。配置中的 `release` 始终优先于默认稳定清单。项目尚未发布稳定版本时，交互向导会要求用户明确确认是否临时使用开发版 `main`；未确认和非交互环境都会停止。

普通用户直接运行一键安装命令。Windows：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Buildlee/agent-memory-gateway/main/scripts/memory-device-install.ps1)))
```

Linux / macOS：

```bash
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/Buildlee/agent-memory-gateway/main/scripts/memory-device-install.sh | sh
```

固定版本发布可在执行前设置 `MEMORY_DEVICE_ARCHIVE_URL` 和 `MEMORY_DEVICE_ARCHIVE_SHA256`；下载摘要不一致时安装会停止。仅开发测试跟随 `main`：Windows 传入 `-Channel development`，Linux/macOS 设置 `MEMORY_DEVICE_CHANNEL=development`。

向导依次询问 Gateway 地址、工作区、需要接入的 Agent 和一次性配对码。它会自动发现 Codex、Hermes、OpenClaw，生成对应格式的 MCP 配置，创建当前用户后台服务，并在首轮真实同步通过后输出 `ready`。配对码只在隐藏输入中使用。命令结束后按 `client_configuration` 列出的路径，把 `shared-memory` 合并到对应 Agent 的 MCP 配置并重启 Agent；向导不会自动覆盖客户端已有设置。

已有受控配置时仍可指定文件或 HTTPS 地址：

```powershell
.\scripts\memory-device-install.ps1 -ProfilePath "C:\secure-share\device-install.json"
.\scripts\memory-device-install.ps1 -ProfileUrl "https://memory-gateway.example.internal/device-install.json"
```

Linux/macOS 可使用 `memory-device onboard --profile ./device-install.json`。中断后加 `--resume`；只生成配置而不启用服务时加 `--no-autostart`。

### 状态、修复、升级、回滚和卸载

```powershell
memory-device status
memory-device doctor
memory-device repair
memory-device repair --apply
memory-device upgrade --package <已校验的源码目录或 wheel> --release-id v0.2.0
memory-device upgrade --package <已校验的源码目录或 wheel> --release-id v0.2.0 --yes
memory-device rollback
memory-device rollback --yes
memory-device uninstall
memory-device uninstall --yes
```

`doctor` 只检查，不修改。`repair` 默认预览修复计划，只补齐权限、缺失的 MCP 配置或停止的受管服务。`upgrade` 默认预览；加 `--yes` 后先在独立版本目录安装并自检，再切换后台服务和 MCP 配置，健康检查失败会自动恢复旧版本。`rollback` 默认预览，加 `--yes` 回到上一个保留版本。`uninstall` 默认也只预览；加 `--yes` 后移除后台服务、运行配置和生成的 MCP 配置，但保留设备凭据、本地记忆以及维护命令本身。

安装器没有配置内 `release` 时使用最新稳定清单；显式开发通道才下载 GitHub 当前 `main`。两种方式都会限制下载体积、保留异常现场，并拒绝覆盖已有缓存、发布目录、密钥、凭据或任务。

没有安装配置或需要处理特殊 Agent 时，仍可使用完整向导。`-Agent` 格式：`安装实例 ID|类型|显示名`，可以重复填写多个：

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" `
  -DefaultWorkspace "shared-workspace" `
  -Agent @(
    "codex-desktop|codex|Codex Desktop"
    "hermes-desktop|hermes|Hermes Desktop"
    "openclaw-desktop|openclaw|OpenClaw"
  ) `
  -InstallAutostart
```

向导提示输入配对码，然后把刷新凭证保存在 Windows Credential Manager。设备私钥、Sidecar outbox key 和本机 MCP 配置在文件已有时会跳过，不自动覆盖。首次执行还会在仓库中建 `.shared-memory-venv`，避免把 MCP 依赖装进全局 Python。带 `-InstallAutostart` 时，向导会在任务启动后用第一条 Agent 身份执行一次同步；认证、工作区授权或 Gateway 网络不通都会直接报错，不会把只有端口存活的状态称为可用。

若配对成功但之后本地准备中断，用原命令加 `-UseExistingCredential` 继续。这要求原设备私钥还在，只复用现有 Windows 凭证。若发现由本安装器创建的 Sidecar 计划任务，恢复时会把它更新为当前设备、Agent 和运行环境；未知任务仍会拒绝替换。凭证不会读取、打印或写入配置文件，已有 MCP JSON 也不会覆盖。

如果 Gateway 使用内部 CA，加 `-GatewayCaCertificate "<CA 证书路径>"`。公网受信任证书不需要此参数；证书不匹配时修正证书链，不要关闭 TLS 校验。

命令结束后列出生成的 MCP JSON 文件。带 `-InstallAutostart` 的成功结果中 `gateway_sync` 为 `ready`；未启用自启时结果为 `configured`，先手动启动 Sidecar 或重新运行并加上 `-InstallAutostart`，再导入 MCP 配置。JSON 只包含本机启动脚本、Agent ID、工作区和本机 key 文件路径，不保存 Gateway 令牌、刷新凭证、数据库地址或私钥。

Docker 中的 Agent 使用同一套身份和工作区协议，但不需要复制桌面运行环境。按[容器内 Agent 的统一接入](container-sidecar.md)运行 `-Mode container`——它会为目标容器建一个只监听容器回环地址的 MCP Bridge。

### 验证连接

配置后，在 Agent 中按这个顺序检查：

1. 调用 `memory_sync_status`，确认 Sidecar 在线并能识别当前 Agent。
2. 调用 `memory_remember` 写入一条测试信息（不含凭证）。
3. 用另一个已授权 Agent 调用 `memory_search` 或 `memory_context` 搜索这条信息。
4. 检查 Gateway 审计记录，确认它们属于预期工作区。

再验证本机任务恢复：调用 `memory_checkpoint` 保存一段不含凭据的任务摘要和下一步，然后调用 `memory_resume`。默认结果只包含摘要骨架；传入 `include_details=true` 才返回决定、引用和元数据。检查点保存在本机加密库，不会自动上传或进入共享长期记忆。

MCP 调用没带 `workspace_id` 时，系统用 `DefaultWorkspace`。没有配置时返回 `WORKSPACE_ID_REQUIRED`；设备或 Agent 不属于该工作区时返回 `WORKSPACE_FORBIDDEN`。这两个错误说明需要补齐或核对授权信息，而不是把工作区名称改成占位文本。

### 单独检查本机 Sidecar

确认计划任务是否还在运行时执行：

```powershell
.\scripts\setup-shared-memory.ps1 -Mode verify
```

只请求 `127.0.0.1` 的 Sidecar 健康接口，不读写记忆、不清 outbox、不连数据库。

---

### 接入 Agent 已有的本机记忆

共享系统不会接管或回写 Agent 原来的记忆库。需要汇入的来源通过环境变量显式配置，例如：

```powershell
$env:MEMORY_LOCAL_PROVIDER_CONFIG = '{"providers":[{"id":"personal-notes","type":"files","display_name":"Personal notes","paths":["<local-memory-file>"]}]}'
```

当前文件 Provider 支持 Markdown、JSON 和 JSONL。第三方记忆系统可以通过 Python 插件入口实现同一 Provider 协议，不需要为每种 Agent 改 Gateway。

在 Agent 中先调用 `memory_local_sources` 查看来源，再用 `memory_local_preview` 预览。`memory_share_selected` 只提交人工选中的记录；`memory_propose_local_candidates` 只会自动提议用户偏好、项目决定、稳定事实和长期约定四类内容。敏感信息、命令式内容和超长内容会被本机拦截，端侧路径不会上传。

日常使用时先调用 `memory_context`。它返回的 `recall_id` 可交给 `memory_feedback`，标记 `useful`、`pin`、`outdated` 或 `incorrect`。反馈只影响后续排序，不会直接删除或改写记忆。

`memory-device doctor` 还会通过正在运行的 Sidecar 检查本机 SQLite 完整性、必需表和少量密文样本。该检查不返回记忆正文，也不执行修复。拥有 `memory.manage` 的 Agent 可用 `memory_list_crystal_candidates` 查看 Worker 发现的结晶候选；重建仍需显式调用 `memory_rebuild_crystal`。

---

## 常见情况

| 提示信息 | 先排查什么 |
|---|---|
| `DemoHome 已存在` | 脚本拒绝覆盖旧数据。指定新 `-DemoHome`，或确认旧演示数据是否还需要。 |
| 端口已被占用 | 指定另一个 `-Port`，例如 `18787`。 |
| 安装依赖失败 | Python 版本、网络、组织包源或 pip 配置。虚拟环境会保留，修复后直接重新运行脚本。 |
| `WORKSPACE_ID_REQUIRED` | Sidecar 和 MCP 启动参数都应提供同一个已登记工作区。 |
| `WORKSPACE_FORBIDDEN` | 管理端尚未把当前设备或 Agent 授予该工作区。 |
| `GATEWAY_UNAVAILABLE` | 本机 Sidecar 未运行、Gateway 地址不可达，或 TLS 证书链未配正确。 |
| MCP 配置已存在 | 安装向导拒绝覆盖。确认现有配置是否还在使用，再选新的 `-McpOutputDirectory`。 |
| 运行环境不完整 | `.shared-memory-venv` 已存在但缺少依赖。脚本不会自动删除它，检查原因后手动处理。 |
| 配对后安装中断 | 保留原设备私钥，用相同参数加 `-UseExistingCredential` 继续，不要再使用已失效的配对码。 |

---

## 下一步

- 需要服务端部署、迁移或上线核对 → [部署说明](deployment.md)
- 需要理解权限、审核、离线同步和检索口径 → [总体设计](design-v2.md)
- 需要看完整的 Codex、Hermes 或 OpenClaw 例子 → [接入示例](../examples/README.md)
