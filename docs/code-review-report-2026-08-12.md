# 代码审查与优化报告（更新于 2026-08-13）

## 1. 审查结论

本次审查以 `origin/main` 的 `8fd4971535a0ca8ea054ba2c4696ee79c11b43ab` 为基线，覆盖 Gateway、Sidecar、导入器、本机安装流程、数据库迁移、反向代理、CI、README 及 `docs/` 下的中英文文档。

按安全性、可靠性、跨平台适配、安装维护体验和文档一致性的顺序完成修改后，Python 全量测试通过，共运行 395 项，跳过 17 项依赖其他平台权限或真实外部环境的测试。当前没有发现会阻止进入代码评审的已知缺陷；真实 PostgreSQL、Caddy、Windows 计划任务、systemd 和 launchd 仍需在发布前做环境验收。

## 2. 已完成的修改

### P0：安全与权限边界

- Gateway 同步接口在原有读写权限之外增加 `memory.sync` 能力校验，避免普通读写令牌调用同步专用接口。
- Gateway 增加请求体大小、请求超时和并发上限；超过容量时返回 503，异常日志只保留追踪编号和必要信息。
- Caddy 配置同步增加 2 MiB 请求体限制和上游超时，代理层与应用层限制保持一致。
- `memory-app` 启动子进程时只传递允许的环境变量，降低令牌、数据库连接串等敏感信息被继承的风险。
- 导入器拒绝不安全的批次名、工作区名和无效 UTF-8 文件，并校验恢复状态中的版本、记录 ID、事件 ID、重复记录和预览摘要。
- 回滚不再信任可编辑状态文件中的后端引用，只接受 Sidecar 本地持久化的事件回执；未完成归档的记录会保留为待处理，而不会误报成功。

### P1：可靠性与可恢复性

- Sidecar 为已同步、已拒绝事件保存最小化回执，导入、恢复和回滚都可以按事件 ID 核对真实状态。
- 导入流程补齐扫描、预览、执行、恢复和回滚路径，并区分成功、拒绝、失败和仍待回滚的记录。
- `memory-app` 增加受控重启策略，避免子进程异常退出后无限快速重启。
- Linux systemd 用户服务和 macOS LaunchAgent 安装支持中断恢复；安装前会检查同名服务，拒绝接管来源不明的现有服务。
- Gateway 和发布地址增加 URL、协议及端口范围校验，端口必须在 1-65535 之间。

### P2：跨平台与维护体验

- 新增 `memory-device` Python 命令，统一 Windows、Linux 和 macOS 的设备安装、状态、诊断、修复和卸载入口。
- Windows 网页一键入口使用 UTF-8 BOM，已兼容系统自带的 Windows PowerShell 5.1；安装后的计划任务直接运行独立 Python 环境，高级维护脚本仍由 PowerShell 7 验证。
- Linux/macOS 提供 POSIX `sh` 一键安装；Linux 使用 systemd 用户服务，macOS 使用 LaunchAgent，凭据和配置使用当前用户专用目录与 `0600` 权限。
- `repair`、`uninstall` 默认只预览，显式确认后才修改；卸载先备份非敏感运行配置，默认保留设备身份、本地记忆和维护命令。
- 修复和卸载校验平台、默认路径、服务标识、MCP 内容以及符号链接，拒绝接管同名未知服务或删除越界路径。
- 安装包限制下载和展开体积，拒绝路径穿越、符号链接、特殊文件和不安全重定向；固定发布支持 SHA-256 校验。
- 自动发现 Codex、Hermes、OpenClaw，并为 OpenClaw 生成 `mcp.servers` 结构；安装器输出导入提示但不覆盖第三方客户端设置。
- 新增数据库迁移，使设备类型约束支持 `windows`、`linux` 和 `macos`；根目录迁移与 Python 包内迁移保持相同内容。
- 新增数据库迁移，使 Agent 类型约束正式支持 `openclaw`。
- GitHub Actions 在 Windows 之外增加 Ubuntu、macOS 测试矩阵。
- Windows CI 覆盖 Python 3.10-3.13，Linux/macOS 覆盖 3.10 与 3.13；独立任务从 wheel 做干净安装和公开 CLI 冒烟测试。
- Wheel 构建前清理受控打包缓存，并拒绝临时模块、缓存、本机绝对路径、重复入口或缺失迁移进入包。
- 默认安装通道改为 GitHub 稳定 Release 清单并校验 SHA-256；开发版 `main` 只能显式选择或在交互向导中确认，不再静默降级。
- 新增标签发布工作流，生成固定源码 ZIP、wheel、两种安装脚本、`release-manifest.json` 和 `SHA256SUMS`；预发布标签会标记为 prerelease。
- 新增 `memory-device upgrade` 与 `rollback`：新版本先安装到独立目录并自检，切换后做 Sidecar 健康检查，失败自动恢复旧运行环境。稳定启动器始终读取当前 `runtime.json`，终端命令与后台服务使用同一版本。

## 3. 代码、架构与文档对应关系

| 架构内容 | 实际实现 | 对应文档 | 核对结果 |
| --- | --- | --- | --- |
| Gateway 鉴权、限流、请求边界 | `gateway.py`、Caddy 配置 | README、部署文档、设计文档 | 一致 |
| Sidecar 本地队列与事件回执 | `outbox.py`、`sidecar_daemon.py`、`sidecar_client.py` | 设计文档、已有记忆导入文档 | 一致 |
| MCP 读写与导入控制 | `sidecar_mcp.py`、`importer.py` | 快速上手、已有记忆导入文档 | 一致 |
| 本机应用启动与重启 | `memory_app.py` | 部署文档、开发文档 | 一致 |
| Windows、Linux、macOS 安装与维护 | PowerShell/sh 脚本、`device_runtime.py`、`device_lifecycle.py` | README、快速上手、部署、设计、示例说明 | 一致 |
| Codex、Hermes、OpenClaw MCP 输出 | `render_mcp_config`、三份示例 JSON | README、快速上手、示例说明 | 一致 |
| 设备类型数据库约束 | 根目录及包内迁移 SQL | 部署文档、开发文档 | 一致 |
| 支持平台与验证范围 | GitHub Actions、自动化测试 | 开发文档 | 一致 |
| 稳定发布、升级和回滚 | Release 工作流、构建脚本、生命周期命令、稳定启动器 | README、快速上手、部署、开发文档 | 一致 |

中英文 README、快速上手、部署、设计、开发和导入文档已同步更新；文档中的命令名、平台差异、权限要求、服务管理方式和测试范围均与当前代码一致。

## 4. 验证结果

| 验证项 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | 395 项通过，17 项跳过 |
| `python -m compileall -q src tests scripts` | 通过 |
| 现有 Python 3.13 开发环境 `python -m pip check` | 未发现依赖冲突 |
| PowerShell 7.6.3 语法解析 | 项目内 17 个 PowerShell 脚本全部通过 |
| Windows PowerShell 5.1 网页入口计划模式 | 通过，UTF-8 BOM 入口可直接运行且无需 PS7 |
| `sh -n scripts/memory-device-install.sh` | 通过 |
| 公开文件敏感信息扫描 | 通过，共检查 216 个文件 |
| `git diff --check` | 通过；仅有 Git 的 LF/CRLF 转换提示 |
| GitHub Actions YAML 结构检查 | 通过，包含 Windows、Ubuntu、macOS 以及标签发布工作流 |
| Wheel 构建及包内容检查 | 通过；未包含临时模块、本机路径或重复命令入口 |
| Release 资产构建和 SHA-256 清单 | 通过；源码 ZIP 条目唯一，安装脚本和稳定启动器齐全 |

本机已安装 PowerShell 7，位置为 `C:\Program Files\PowerShell\7\pwsh.exe`；所有 PowerShell 脚本已由其 AST 解析器验证，一键入口的计划模式也在 PowerShell 7 下通过。Python 全量测试由项目正式入口 `unittest` 执行，共 395 项通过、17 项跳过。wheel 使用项目 Python 3.12 虚拟环境无隔离构建并通过内容检查，Release 资产与 `SHA256SUMS` 也完成本机验证。本轮没有实际注册计划任务、安装 systemd/launchd 服务、配对真实设备、删除现场数据或连接生产数据库。

Scoop Python 3.13 缺少 `build` 和 `setuptools`，尝试从 PyPI 补装时先后遇到网络权限和本机代理/TLS 中断；没有关闭 TLS 校验。项目 Python 3.12 `.venv` 已具备完整打包后端，因此用于本机 wheel 与 Release 验证。多版本完整依赖回归由新增 GitHub Actions 矩阵确认。

## 5. 发布前仍需确认

1. 在 GitHub Actions 上确认 Windows、Ubuntu、macOS 三个平台和 wheel 干净安装全部通过。当前改动尚未提交，远端 CI 还没有运行这份工作区代码。
2. 在测试 PostgreSQL 上备份后执行新增迁移，确认旧设备记录不受影响，再安排生产迁移窗口。
3. 使用测试域名加载 Caddy 配置，验证 2 MiB 请求限制、上游超时和管理页面代理。
4. 分别在 Windows、Linux、macOS 测试设备上完成安装、重启、恢复、修复预览、卸载预览和确认卸载，确认计划任务、systemd、launchd 的实际行为与单元测试一致。
5. 远端当前只有归档标签，没有稳定 Release。合并后先更新版本号并创建首个正式 `v*` 标签，确认 Release 资产完整后再把一键安装作为生产入口推广。
6. 合并前复核本报告列出的改动范围，再决定是否拆分为安全、导入、跨平台安装、发布机制和文档提交，降低回滚成本。

## 6. 当前状态

所有修改仍在本地工作区，未提交、未推送，也未创建 Pull Request。原有功能测试全部通过，没有执行数据库写入、服务安装或远端部署。
