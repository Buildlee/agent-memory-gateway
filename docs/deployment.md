# 部署说明

本文给出不包含现场信息的通用部署流程。所有示例中的域名、路径、设备 ID、数据库连接串和密钥都必须替换为你自己的本地配置；不要把真实值提交到 Git。

## 1. 部署前检查

- Python 版本不低于 3.10。
- 生产环境准备 PostgreSQL、容器运行时和 HTTPS 反向代理。
- Gateway、Worker、数据库和 Agent 客户端使用最小权限账号或受限网络。
- 准备独立的密钥：事件加密、令牌签名、刷新重放保护、敏感信息指纹和 Sidecar outbox 不能复用同一个值。
- 先备份既有数据库和运行配置，再执行任何迁移。

仓库只保留 `deploy/fn/.env.example` 这类变量示例。复制后形成的环境文件、主体配置、证书和私钥必须留在受保护的本机目录，并已被 `.gitignore` 排除。

## 2. 安装依赖与自检

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src
```

测试通过只说明代码基线可用，不代表生产配置已经正确。

## 3. 数据库迁移

生产 Gateway 使用独立的元数据库。先执行只读检查：

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --check
```

确认版本、扩展、权限和备份均正确后，才执行：

```powershell
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --apply
memory-gateway migrate --metadata-dsn $env:MEMORY_METADATA_DSN --verify
```

如果接入可选的 PostgreSQL 长期记忆后端，也要先做对应检查，再执行专用迁移：

```powershell
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --check
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --apply
memory-gateway gbrain-migrate --gbrain-dsn $env:MEMORY_GBRAIN_MIGRATOR_DSN --verify
```

不要把迁移命令接到 Gateway 启动脚本中。运行账号不应拥有建库、建角色或 schema 管理权限。

## 4. 容器化运行

仓库提供一份 Gateway、Worker 和 HTTPS 代理的 Compose 配置。运行前将示例环境文件复制到受保护位置，并明确传入路径：

```powershell
docker compose --env-file "<受保护环境文件路径>" -f deploy/fn/compose.yaml config
docker compose --env-file "<受保护环境文件路径>" -f deploy/fn/compose.yaml up -d --build
```

先执行 `config`，确认没有把密钥、数据库端口或不应暴露的卷映射到公共网络。Gateway 和 Worker 应使用同一版本镜像，但只由代理暴露 HTTPS 入口；数据库不映射到宿主机公开端口。

## 5. 客户端 Sidecar

每台设备只启动一个 Sidecar。它管理本机凭据、加密 outbox 和本机 RPC，Agent 通过 MCP 调用它：

```powershell
.\scripts\start-sidecar.ps1 `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "your-registered-device-id" `
  -AllowedAgents "your-agent-installation-id"
```

Sidecar 必须只监听本机回环地址。局域网内直连内部 HTTPS 地址；外网访问通过 VPN、零信任网络或受控隧道回到同一网络边界。无论何种网络路径，都不要关闭 TLS 证书校验。

## 6. 上线验证

按顺序检查：

1. Gateway 和 Worker 的健康检查通过。
2. 数据库迁移 `verify` 通过，运行账号权限符合最小权限原则。
3. 已登记设备可以取得短期令牌，未登记设备被拒绝。
4. 一个 Agent 能写入、搜索和获取上下文；另一个已授权 Agent 能看到同一作用域结果。
5. 断开客户端网络后，事件进入加密 outbox；恢复网络后只同步一次。
6. 提交冲突候选并完成一次审核、撤销或归档，确认审计记录存在。
7. 检查日志、MCP 配置、Compose 配置和 Git 暂存区，确认其中没有真实密钥、令牌、连接串、证书或本机路径。

## 7. 升级与恢复

- 升级前记录当前镜像版本和迁移版本，备份数据库与受保护配置。
- 先在副本或维护窗口执行 `check`，再执行新增迁移和 `verify`。
- 发生失败时不要反复重放写入脚本；先检查事件账本、固定回执、死信和后端引用，再由 Worker 对账。
- 凭据轮换顺序是：生成新值、更新消费者、验证、撤销旧值。不要直接覆盖仍在使用的密钥。

现场操作记录应保存在忽略文件中，不应随代码上传。
