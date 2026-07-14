# 把已有本地记忆接入共享库

本机已经积累了很多记忆时，先把它们作为待审核材料导入，不直接当作共享事实写入。

导入时会经过这些步骤：

```text
本地已有记忆
  -> Memory Importer
  -> Staging Area 暂存区
  -> 敏感信息扫描
  -> 切块与分类
  -> 作用域归属判断
  -> 去重 / 合并 / 冲突检测
  -> 记忆结晶
  -> 写入 Gateway
```

## 导入时要守住的几件事

- 旧记忆默认进入 `imported_candidate`。
- 每条记忆保留来源路径、hash、批次号。
- 导入必须可预览、可回滚。
- 敏感信息必须先拦截。
- 旧记忆不能自动覆盖新决策。

## 常见文件通常归到哪里

| 来源 | 默认 Scope |
|---|---|
| `USER.md` | `user` |
| `MEMORY.md` | `workspace` 或 `agent` |
| `SOUL.md` | `agent` 或 `private` |
| 本机路径、端口、硬件记录 | `device` |
| 项目架构决策 | `workspace` |
| 旧任务状态 | `session` 或 `archived` |

## 当前可以先做的操作

```powershell
memory-import scan --source .\memory-folder --batch import_2026_07_03
```

扫描结果会生成 JSONL 预览文件。后续版本会加入 `preview/apply/rollback`。
