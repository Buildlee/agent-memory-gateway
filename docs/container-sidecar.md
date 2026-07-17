# 容器内 Agent 的统一接入

本流程适用于任何运行在 Docker 内的 Agent。不要求为 NAS、Linux 发行版或特定模型单独改代码。

安装器只需要三件事：哪个容器要使用记忆、它的持久状态目录、要登记的设备和 Agent 身份。其余信息从容器标签自动识别（Compose 项目、服务名、Gateway 发布副本）。

---

## 安装器产生的文件

安装器生成三份凭据文件，全部放在 `ContainerStateDirectory`，权限 `0600`：

- `device-identity.pem` — 设备私钥
- `refresh-credential.json` — 受限刷新凭据
- `sidecar.env` — Sidecar outbox key 和状态

凭据只存在于该目录，不会写入 MCP 配置、容器环境变量或 Git。

安装器随后为新 Agent 追加所选工作区的最小权限绑定，启动 `memory-mcp-bridge` 容器。

---

## Bridge 网络模型

Bridge 与目标容器共用网络命名空间：

- 地址：`http://127.0.0.1:8767/mcp`
- 协议：Streamable HTTP MCP
- 不绑定宿主机端口
- Gateway、数据库、刷新凭据不暴露给局域网

---

## 运行安装器

从 Gateway 能访问的部署机执行。所有名称是占位符；状态目录必须是目标容器所在宿主机上的持久路径。首次运行创建目录，已有凭据时安装器停止（不覆盖）。

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode container `
  -SshHost "deploy-user@nas" `
  -SshPort 22 `
  -ClientContainerName "agent-webui" `
  -ContainerStateDirectory "/srv/agent-memory/agent-webui" `
  -TenantId "personal" `
  -UserId "owner" `
  -DeviceId "nas-agent" `
  -DeviceName "NAS Agent" `
  -DeviceType nas `
  -Agent "agent-nas|hermes|NAS Agent" `
  -DefaultWorkspace "shared-workspace" `
  -Apply
```

### 必需参数

| 参数 | 说明 |
|------|------|
| `-SshHost` | 目标宿主机 SSH 地址 |
| `-ClientContainerName` | 目标容器名 |
| `-ContainerStateDirectory` | 宿主机上的持久状态目录（Linux 绝对路径） |
| `-TenantId` | 租户 ID |
| `-UserId` | 用户 ID |
| `-DeviceId` | 设备 ID |
| `-DefaultWorkspace` | 已登记的工作区 ID |
| `-Agent` | 三段式 `实例ID\|类型\|显示名` |

### 可选参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-SshPort` | SSH 端口 | 22 |
| `-DeviceName` | 设备显示名 | 等于 `-DeviceId` |
| `-DeviceType` | `nas` 或 `other` | `nas` |
| `-ContainerCapabilities` | 逗号分隔的能力列表 | 不含 `memory.manage`（见下方） |
| `-ContainerGatewayUrl` | 容器内 Gateway 地址 | `http://gateway:8787` |

---

### 默认权限

默认授权包含读写、搜索、同步、反馈和遗忘，**不包含** `memory.manage`：

```
memory.feedback,memory.forget,memory.read_context,
memory.search,memory.sync,memory.write_event
```

需要管理权限时由管理员明确传入 `-ContainerCapabilities`。不要把管理能力作为普通客户端的默认值。

---

## 命令模式

### `-Apply`（首次安装）

不带 `-Apply` 时脚本只做识别和报告：确认容器、Compose 项目、Gateway 和 MCP 地址；检查目标容器和 Bridge 状态；不创建目录、不配对、不改数据库、不启动容器。

执行 `-Apply` 前脚本确认目标 Agent 容器正在运行，避免把 Bridge 健康误判为 Agent 可用。

### `-Resume`（从中断恢复）

配对或 key 生成中断时，相同参数加 `-Resume`。脚本复用已有受保护文件，不重置设备身份。

---

## 追溯容器

每次接入保留两个已停止的启动容器，方便追溯安装结果：

- `memory-sidecar-pair-<suffix>` — 设备配对阶段
- `memory-sidecar-key-<suffix>` — key 生成阶段

它们不保存刷新凭据或私钥。真正的状态只在 `ContainerStateDirectory`。清理这两个容器属于删除操作，确认后单独处理。

---

## 在 Agent 中添加 MCP 服务器

在目标 Agent 的 MCP 管理界面或配置 API 中新增服务器：

```text
名称：shared-memory
传输：Streamable HTTP
地址：http://127.0.0.1:8767/mcp
```

`127.0.0.1` 指向目标容器的网络命名空间，不是部署电脑。

安装器不会修改应用数据库，也不绕过应用登录。只要客户端支持标准 Streamable HTTP MCP，地址和工具集合不变。

---

### 验证步骤

保存配置后，安装器自动执行三项验证：

1. Bridge → Gateway 只读同步（`LocalSidecarProxy.health()` + `sync()`）
2. MCP initialize 握手（`http://127.0.0.1:8767/mcp`）
3. 工具调用 `memory_sync_status`（确认 Agent 管理界面已加载新服务器）

---

## 平台差异

Windows 和容器设备走同一条身份、授权、Sidecar 和 MCP 协议。差异只在于本地安全存储和守护方式：

| 维度 | Windows | 容器 |
|------|---------|------|
| 凭据存储 | Credential Manager | `0600` 文件 |
| 进程生命周期 | 计划任务 | Docker Bridge 容器 |

设备型号、NAS 品牌、Agent 厂商不进入 Gateway 的业务规则。

如果一个 Agent 没有标准 MCP 配置入口，可单独为其编写配置连接器。连接器只负责把 MCP 地址写入官方配置接口，不能保存刷新凭据、直接访问数据库或改变工作区授权。
