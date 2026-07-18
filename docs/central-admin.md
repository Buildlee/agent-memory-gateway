# 中枢管理页

管理页的运行位置应当跟随共享记忆的中枢：部署在 Gateway、Worker 和元数据存储所在的飞牛环境，而不是固定依赖某一台 Windows 电脑。这样设备状态、审核、死信和活动记录都从同一个服务边界读取；本机仍可保留回环管理页，作为离线维护时的备用入口。

```mermaid
flowchart LR
  B["浏览器"] -->|"HTTPS /admin + 一次性会话"| P["Caddy 反向代理"]
  P --> C["中枢 Admin Console"]
  C -->|"同一网络命名空间"| S["中枢管理 Sidecar"]
  S -->|"短期令牌 + memory.manage"| G["Memory Gateway"]
  G --> M[("元数据、审计与记忆后端")]
```

浏览器只访问 Caddy 的 `/admin` 路径。管理控制台不会连接数据库，也不保存 Gateway 令牌、刷新凭据或设备私钥；它只能通过中枢管理 Sidecar 调用 Gateway 已有的授权接口。

## 首次配置

先发布包含 `deploy/fn/admin-console.compose.yaml` 的 Gateway 版本，并确认 Gateway、Worker 和代理健康。然后在 Windows 上先做只读预检：

```powershell
.\scripts\setup-central-admin.ps1 `
  -SshHost "deploy-user@nas" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -StateDirectory "/srv/memory-gateway/admin" `
  -TenantId "tenant" `
  -UserId "administrator" `
  -DeviceId "memory-admin" `
  -AgentInstallationId "memory-admin" `
  -DefaultWorkspace "shared-workspace" `
  -PublicBaseUrl "https://memory-gateway.internal:8443/admin"
```

预检只核对 Gateway、发布副本、Docker 网络、目标目录和现有中枢管理容器，不会创建身份、写入凭据或替换容器。确认输出正确后，在同一命令末尾加 `-Apply`。首次执行会：

- 登记一个独立的中枢管理设备与 Agent，并只授予指定工作区的能力；默认包含 `memory.manage`。
- 将设备密钥、刷新凭据和 Sidecar key 分别写入受保护目录，只允许所有者访问。常见 Linux 文件系统显示为 `0600`；部分 NAS 挂载会显示为等价的 `0700`。这些内容不会打印或进入 Git。
- 只启动 `admin-sidecar` 和 `admin-console` 两个中枢容器，不修改 Hermes、数据库或现有 Agent Bridge。

已有中枢管理身份或容器时，脚本默认停止并要求先核对。只有确认要替换这两个管理容器时，才显式加 `-Resume`。

## 打开页面

不要把长期密码、Gateway token 或固定浏览器链接写进配置。每次打开页面，都运行：

```powershell
.\scripts\open-central-admin.ps1 `
  -SshHost "deploy-user@nas" `
  -SshPort 22 `
  -RemoteRoot "/srv/memory-gateway" `
  -StateDirectory "/srv/memory-gateway/admin"
```

该脚本只重建 `admin-console`，产生新的短期一次性链接后直接交给默认浏览器；链接不会回显到 PowerShell、操作记录或 Docker 日志。首个浏览器请求会把它换成 `HttpOnly`、`Secure`、`SameSite=Strict` 的会话 Cookie，路径限定在 `/admin`。重新打开时会得到一条新链接。

## 网络与权限边界

- Caddy 是唯一对浏览器开放的入口；`admin-console` 没有宿主机端口，`admin-sidecar` 的 RPC 只监听容器回环地址。
- 只在内网或已接入 VPN 的网络边界访问 `/admin`。不要把该路径映射到公共互联网，也不要关闭 TLS 校验。
- 中枢管理身份和 Codex、Hermes 的身份彼此独立。它们使用同一套设备登记与工作区授权模型，不需要为某个机型写专用管理页。
- 页面只展示已授权的设备、能力、状态、时间、事件引用和审计元数据；不展示公钥原文、刷新凭据、连接串、令牌或记忆密文。

## 验收

1. Gateway、Worker、代理和 `admin-sidecar` 都处于 healthy/running。
2. 通过打开脚本进入 `/admin`，确认概览、审核、设备、运行和活动接口均能读取。
3. 从浏览器确认状态卡能进入审核、运行或设备详情；设备页能看到设备、Agent、绑定、能力和最近状态，但没有凭据。
4. 以一个明确确认的审核动作验证 Gateway 审计；不把删除、批量清理或自动重放加入管理页。

若中枢入口不可用，先检查 Caddy、`admin-sidecar` 健康和 Gateway 授权，再执行打开脚本。不要通过浏览器直连数据库或改写 Hermes 配置库来绕过这条路径。
