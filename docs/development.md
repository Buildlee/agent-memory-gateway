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
