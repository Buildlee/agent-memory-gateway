# 容器内 Agent 的统一接入

这一条流程适用于任何运行在 Docker 里的 Agent。它不关心容器里跑的是哪一种产品，也不要求为 NAS、Linux 发行版或某个模型单独改代码。

安装器只需要知道三件事：哪个容器要使用记忆、它的持久状态目录放在哪里、要登记的设备和 Agent 身份。其余信息会从正在运行的容器标签中识别，包括 Compose 项目、服务名和当前 Gateway 发布副本。

## 这套适配器做了什么

安装器会生成一份设备私钥、受限的刷新凭据和 Sidecar outbox key。它们只保存在指定的持久目录，权限必须是 `0600`，不会写入 MCP 配置、容器环境变量或 Git。随后安装器为新 Agent 追加所选工作区的最小权限绑定，并启动一个通用 `memory-mcp-bridge` 容器。

Bridge 与目标容器共用网络命名空间，只在容器内的 `127.0.0.1:8767` 提供 Streamable HTTP MCP。它没有宿主机端口，也不会把 Gateway、数据库或刷新凭据暴露给局域网。

## 运行安装器

先从 Gateway 能访问的部署机运行下面的命令。示例里的名称都是占位符；状态目录必须是目标容器所在宿主机上的持久目录，首次运行会创建它，已有凭据时安装器会停止而不是覆盖。

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

不带 `-Apply` 时，脚本只识别容器、Compose 项目、Gateway 和最终 MCP 地址，同时报告目标容器和 Bridge 的状态；不会创建目录、配对设备、改数据库或启动容器。执行 `-Apply` 或 `-Resume` 前，脚本会确认目标 Agent 容器正在运行，避免把 Bridge 健康误判为 Agent 已可用。配对完成后中断时，使用相同参数加 `-Resume`；它只复用已有的受保护文件，不会重置设备身份。

配对和 key 生成会各保留一个已停止的受限启动容器，方便追溯安装结果。它们不保存刷新凭据或私钥，真正的状态只在指定目录中。清理这些启动容器属于删除操作，应在确认后单独进行。

默认授权包含读写、搜索、同步、反馈和遗忘，不包含 `memory.manage`。需要管理权限时，应由管理员明确传入 `-ContainerCapabilities`，不要把管理能力作为普通客户端的默认权限。

## 在 Agent 中添加 MCP 服务器

在目标 Agent 自己的 MCP 管理界面或官方配置 API 中新增一个服务器：

```text
名称：shared-memory
传输：Streamable HTTP
地址：http://127.0.0.1:8767/mcp
```

这里的 `127.0.0.1` 指向目标容器的网络命名空间，不是部署电脑。不同 Agent 的设置页面和认证方式不一样，因此安装器不会修改应用数据库，也不会尝试绕过应用登录。只要客户端支持标准 Streamable HTTP MCP，这个地址和工具集合不变。

保存后，先调用 `memory_sync_status`。安装器会验证 Bridge 到 Gateway 的只读同步、MCP 初始化和这项实际工具调用；这一步确认 Agent 的管理界面也已实际加载新服务器。

## 统一接口，平台只做最少差异处理

Windows 和容器设备走同一条身份、授权、Sidecar 和 MCP 协议。差别只有本地安全存储和守护方式：Windows 使用 Credential Manager 与计划任务，容器使用权限为 `0600` 的持久目录和 Bridge 容器。设备型号、NAS 品牌和 Agent 厂商不进入 Gateway 的业务规则。

如果一个 Agent 没有标准 MCP 配置入口，可以单独做它的配置连接器。连接器只负责把上面的地址填进该产品的官方配置接口，不能保存刷新凭据、直接访问数据库或改变工作区授权。
