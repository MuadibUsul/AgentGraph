# AgentGraph 安全审计报告 — 2026-10-20

**审计对象：** `E:\Codes\AgentGraph`，生产 `https://agentgraph.fusionpropulsion.space`
**审计方式：** 攻击者视角静态审计 + 本地一次性 pgvector 环境复现（未对生产做破坏性测试）+ 生产只读配置/响应头核验
**结论：** 当前生产配置下未发现“无需认证直接接管”的 Critical 漏洞；但存在 **8 组 High**，其中 4 组已在一次性测试工作区复现。最严重的问题不是数据库 RLS，而是**应用层的 namespace 边界和人类审批门禁没有真正闭合**。

---

## 1. 风险总览

| 编号 | 严重度 | 问题 | 状态 |
|---|---|---|---|
| H1 | High | namespace 隔离在多条读取链路上失效：提案、审计、时间线、任务上下文、链接、SSE、外部动作 | 已复现 4 条 |
| H2 | High | 人类审批门禁可被绕过：workspace key 处理升级任务、Agent 自行 DONE、AWAITING_HUMAN 可被 AUDITOR 处理、human_feedback 可伪造 | 已复现 2 条 |
| H3 | High | 只读 VIEWER 可以清除/设置文档污点，洗白不可信内容 | 已复现 |
| H4 | High | Webhook/外部动作的 SSRF 校验存在 TOCTOU/DNS rebinding；Webhook 发送与测试完全跳过校验；审批界面看不到真实 payload/headers | 代码确认 |
| H5 | High | 邮箱预注册 + Google 按 email 自动绑定，可形成账号预劫持；当前 Google 未开启，属于待启用风险 | 代码确认 |
| H6 | High | 未认证的注册接口可被用于 scrypt CPU 阻塞 + 无限账号/工作区制造；重复邮箱失败会回滚限流计数 | 代码确认 |
| H7 | High | 文档写入/批量写入/重建索引没有嵌入预算，可耗尽共享 embedding 配额并拖垮 DB 连接池 | 代码确认 |
| H8 | High | REVIEWER 被映射为 COMMANDER，能改预算/策略、建 Webhook；新 Webhook 从事件 0 重放历史 | 代码确认 |
| M1 | Medium | 管理员令牌/会话放在 localStorage，配合无 CSP/无 X-Frame 的运营端，XSS 或扩展即可全局接管 | 代码/生产头确认 |
| M2 | Medium | RLS 激活失败是 fail-open，且应用用 PostgreSQL 超级用户连接 | 代码确认 |
| M3 | Medium | 草稿/污点“默认不可见”只是查询过滤；census、直接读取、历史版本都可绕过 | 代码确认 |
| M4 | Medium | Archive/restore 不检查 `locked_by_human`，创建者可隐藏被人类锁定的文档 | 代码确认 |
| M5 | Medium | 审计身份可伪造：workspace key 可选择任意 actor/agent，审计不记录 token/user/auth_kind | 代码确认 |
| M6 | Medium | MCP 和 SSE 绕过 per-principal 限流；SSE 无连接数/时长上限 | 代码确认 |
| M7 | Medium | 审计/文档列表/增量地图单次响应可达数十 MB，缺少聚合字节上限和保留策略 | 代码确认 |
| M8 | Medium | 账号生命周期缺陷：无邮箱验证/自助重置/自助改密，临时密码长期有效；注册和登录可枚举邮箱 | 代码确认 |
| L1 | Low | 生产 docs/CORS 已收紧，但 `.env.example` 默认 `DOCS_REQUIRE_AUTH=false`、`CORS_ORIGINS=["*"]`、示例 ADMIN_TOKEN | 配置风险 |
| L2 | Low | 生产响应缺少 HSTS/CSP/X-Frame/`X-Content-Type-Options`/`Referrer-Policy` | 生产头确认 |
| L3 | Low | Markdown 允许外部图片/链接，可被用于追踪运营人员或钓鱼 | 代码确认 |
| L4 | Low | `backups/` 未从 Docker build context 排除；基础镜像未按 digest 固定；Next/PostCSS 有审计告警但当前静态导出不适用 | 代码确认 |

---

## 2. 主要攻击链

### 攻击链 A：受限 Agent → 跨 namespace 拿到全工作区内容

前提：攻击者拿到一个 `allowed_namespaces=["raw_inbox"]` 的 SCOUT/ANALYST/AUDITOR token。这在多 bot 部署里是常规配置。

1. SCOUT 调 `GET /v1/proposals`。`list_proposals` 只过滤 workspace，不按文档 namespace 过滤；返回的 `ProposalOut` 包含完整 `content`/`frontmatter`。攻击者直接读到其他 namespace 的提案正文。
2. ANALYST 调 `GET /v1/timeline?namespace=<secret>`。路由把原始 namespace 字符串传给服务，没有 `ensure_namespace`；服务只按 workspace 过滤，攻击者可读其他 namespace 的 handoff/review/失败事件 payload。
3. AUDITOR 调 `GET /v1/audit?limit=500`。`state_audit_diffs` 的 before/after 是完整文档快照，包含正文、frontmatter、human note；服务只过滤 workspace。
4. EXECUTOR 创建任务时在 `input_context` 里写入其他 namespace 的 `slot_key`；claim 后返回 `slots[].current_state`，因为 `_context_slots` 只按 workspace + slot_key 查询。
5. 通过 `POST /v1/links` 把本 namespace 任务与另一个 namespace 的任务连成 `depends_on`，claim 时 `_upstream_briefs` 返回对方 handoff brief；handoff 到 DONE 时 `_release_dependents` 还能把对方 TODO 任务改成 READY。
6. SSE 的 `_event_allowed` 只对 `shared_documents`/`timeline_streams` 做 namespace 过滤，`context_slots`/`collab_tasks`/`document_proposals`/`external_actions` 的元数据会跨 namespace 推给订阅者。

**影响：** namespace 是产品对外承诺的租户内隔离边界（SCOUT 的 raw_inbox、团队/项目隔离）。这条链使“受限 Agent”可以拿到其他 namespace 的正文、状态和任务 brief，且不需要任何写入权限升级。

**复现证据：** 本地一次性测试工作区中，SCOUT 读到 `secret-*` namespace 的提案正文；AUDITOR 读到该 namespace 的文档正文；ANALYST 读到该 namespace 的 timeline payload；EXECUTOR 通过任务上下文读到该 namespace 的 slot 状态。四项均返回 200 并命中秘密标记。

**修复：** 把 namespace 校验做成服务层统一入口，而不是逐路由补：
- `list_proposals` 必须 join `shared_documents` 或在 `document_proposals` 增加 namespace 列；`get_proposal`/`vote_proposal` 先解析文档并 `ensure_namespace`。
- `list_audit` 需要按 target 表解析 namespace，或仅允许 COMMANDER/`*` 读取。
- `list_timeline` 接收 Principal，缺省时强制 `namespace IN allowed_namespaces`。
- `_context_slots`、`_upstream_briefs`、`_release_dependents`、`list_links` 全部使用同一 namespace 过滤。
- SSE 为每种事件类型建立 table→namespace/permission 映射，不要只做 SCOUT 特判。

### 攻击链 B：低权限人类/自动化 key 绕过“人类审批”

1. **workspace key 处理升级任务：** `review_task` 只阻止“非 commander”处理 `ESCALATED_TO_HUMAN`；workspace API key 的 role 是 `COMMANDER`，因此可以通过 `/v1/tasks/{key}/review` 直接 approve/reject。`reviewed_by` 还来自 `effective_agent(principal, request.agent)`，workspace key 可把它写成任意人类邮箱。
2. **Agent 自行 DONE：** `handoff_task` 的 `next_status` 允许 `DONE`；从 IN_PROGRESS handoff 到 DONE 不需要 review。任务会释放依赖任务；`human_feedback` 字段直接取 Agent 请求体，可伪造“human approved”。
3. **AUDITOR 处理 AWAITING_HUMAN：** `review_task` 只对 `ESCALATED_TO_HUMAN` 加 commander 限制；`AWAITING_HUMAN` 对拥有 `task:review` 的 AUDITOR 开放，Agent 可以处理本应等待人类的任务。
4. workspace key 在 approve 时还能触发 `verify_artifact_for_review`；未 tainted 的文档会被标成 `VERIFIED/TRUSTED`，把“人类审批”变成自动化 key 的自我认证。

**影响：** 这是产品核心卖点“human-in-the-loop”的直接绕过；审批记录还可能被伪造成人类身份，事后审计无法区分。

**修复：** 对 `ESCALATED_TO_HUMAN` 和 `AWAITING_HUMAN` 统一要求 `principal.is_human`（admin console 的 HUMAN_ADMIN 可通过）；`TaskHandoffRequest` 对非人类拒绝 `DONE`；`human_feedback` 只允许人类写；审计记录增加 `auth_kind/token_id/user_id`，不要只存字符串。

### 攻击链 C：Agent 提议外部动作 → 人类“盲批” → SSRF/数据外发

1. EXECUTOR 创建 `target_url=https://attacker.tld/x` 的 action，`action_type` 和 URL 看起来正常；`payload`/`headers` 不出现在 `ExternalActionOut`，审批界面只显示 URL、方法、状态和 compensation payload。
2. 人类看到“合法目标”后 approve；Agent 的隐藏 payload 里可以放工作区数据，或 headers 里放 `Host`/`Authorization`。
3. 派发器 `process_actions` 先 `validate_target`，再 `send_json`；两次分别做 DNS 解析，没有 IP pinning。攻击者用 TTL=0 的 DNS 在两次解析间把域名切到 `172.x/10.x/127.0.0.1`，即可 SSRF。
4. Webhook 更直接：`test_rule` 和 `process_deliveries` 完全不调用 `validate_target`，只在创建/更新时校验一次；创建后改 DNS 就能把投递打到内网。
5. `_is_private` 只检查 `is_private/is_loopback/is_link_local/...`，漏掉 `100.64.0.0/10`（阿里云 metadata `100.100.100.200`、CGNAT、部分 Tailscale 地址）；已用 Python 标准库验证这些地址 `is_private=False`。
6. 补偿动作自动 `APPROVED`：Agent 在 primary action 里塞 `compensation_payload`，人类之后正常 undo 一个 batch 就会自动执行补偿 POST，没有第二次审批。

**影响：** 一次 human approval 可被用来打内网、访问云 metadata/CGNAT 服务，或把工作区数据发到攻击者控制的合法域名；审批界面没有展示真正要发送的 body，批准并不构成知情同意。

**修复：** 在 `send_json` 内部强制校验并固定解析 IP（或 connect 时校验 peer IP）；对 webhook delivery/test 每次重校验；拒绝非 `ip.is_global` 地址；审批 API 返回脱敏后的 payload/headers/body digest；补偿动作改为 `PENDING_APPROVAL` 或在 undo 确认页显示完整补偿 body；限制响应体大小和总超时。

### 攻击链 D：未认证攻击者 → 账号/嵌入资源耗尽

1. `POST /v1/auth/register` 对每个请求同步执行 `hashlib.scrypt`（16 MiB、约 40–50 ms），且该函数阻塞 FastAPI 事件循环。
2. 用已存在的邮箱重复注册时，`_rate_limit_login` 先写入计数，随后 `_provision_account` 抛 409；`get_db` 捕获异常后 rollback，**限流计数也一起回滚**。攻击者可无限重复，每次都消耗一次 scrypt。
3. 注册没有邮箱验证、没有全局 IP 配额，唯一邮箱即可创建新 workspace；攻击者可以批量制造租户、会话、审计行和 rate-limit 行。
4. 文档写入没有调用 `assert_budget`：每次 create/update/patch 都调用 `_embed`，`reindex` 还能一次重嵌最多 500 个文档；任何免费 OWNER 都能无限重复写同一文档或调 `/v1/human/reindex`，消耗共享 Jina 配额、长时间占用 DB 连接（网络等待发生在事务内）。
5. MCP 的 `_principal_for_call` 和 SSE 的 `get_stream_principal` 都没有调用 `enforce_rate_limit`；SSE 也没有连接数/时长上限。

**影响：** 单核 CPU 被 scrypt 打满、API 整体不可用；共享 embedding 配额被耗尽导致所有租户语义检索失败；DB 连接池被长事务占满；大量租户/行导致数据库和备份持续膨胀。这是跨租户的可用性和成本攻击。

**修复：** scrypt 放到线程池并加全局并发信号量；限流计数独立事务提交，邮箱存在性检查放在 hash 之前；注册加邮箱验证/CAPTCHA/全局 IP 与子网配额；文档/embedding 路径接入硬预算和每租户并发限制；把 embedding I/O 移出 DB 事务；MCP/SSE 统一走限流。

### 攻击链 E：邮箱预注册 → 账号预劫持（Google 开启后）

1. 攻击者用 `victim@company.com` 注册。系统不验证邮箱，立即创建 workspace，攻击者成为 OWNER 并设置密码。
2. 受害者之后用 Google 登录同一地址。`google_login` 只按 `email` 查 `HumanUser`，不校验 `sub`、不要求邮箱所有权确认，直接签发该用户所属 workspace 的会话。
3. 攻击者仍保留原密码和已创建的 agent token，可继续进入同一 workspace；受害者没有自助重置流程，无法夺回。
4. 即使受害者不使用 Google，攻击者也可以用全局唯一 email 抢占注册，导致受害者注册 409 且无法重置密码。

当前生产 `google_enabled:false`，所以第 2 步尚未开放；但代码已经准备启用，必须在启用 Google 前修复。

**修复：** 增加 `email_verified`、`google_sub`，按 `sub` 绑定；Google 登录不得自动绑定未验证的密码账号；注册先验证邮箱再创建 workspace；提供一次性、限时的重置令牌；重复注册返回统一响应，避免枚举。

---

## 3. 详细发现

### H1 namespace 隔离（High，已复现）

| 链路 | 证据 | 后果 |
|---|---|---|
| 提案列表/详情/投票 | `app/services/proposals.py:209-247`, `250-307`；`app/api/v1_routes.py:502-539` | SCOUT 可读其他 namespace 的完整 proposed content；可跨 namespace 投票 |
| 审计 | `app/services/audit.py:98-157`；`common.py:263-286` | AUDITOR 可读其他 namespace 的完整 before/after 文档/槽位/任务 |
| 时间线 | `app/services/timeline.py:57-80`；`v1_routes.py:815-839` | ANALYST/EXECUTOR 可读任意 namespace 的 payload |
| 任务上下文 | `app/services/tasks.py:241-264`, `520-526` | EXECUTOR 通过 `input_context.slot_keys` 读其他 namespace 的 slot state |
| 链接 | `app/services/links.py:95-174`；`tasks.py:207-238`, `602-674` | 可创建跨 namespace 边、读 upstream brief、改对方任务状态 |
| SSE | `app/api/stream_routes.py:97-122` | 跨 namespace 元数据；SCOUT 仍收到 task/proposal/action 事件 |
| 外部动作 | `app/services/external_actions.py:57-138`；MCP `server.py:991-1026` | 可在别的 namespace 创建 action；list 返回全部 target URL/compensation；MCP 绕过 `action:propose` |

**最小修复：** 在服务层引入 `resolve_namespace(document/task/slot/link)` 并统一调用；为 `document_proposals` 增加 namespace 列或 join；SSE 按表映射权限；MCP 每个工具入口显式 `require_permission` + `ensure_namespace`。

### H2 人类门禁（High，已复现两条）

- workspace key 处理 `ESCALATED_TO_HUMAN`：`services/tasks.py:819-872`；`auth.py:59-67`（workspace key `is_human=False` 但 `is_commander=True`）。本地 probe 通过。
- Agent handoff 到 DONE + 伪造 `human_feedback`：`schemas.py:185-199`；`services/tasks.py:740-781`。本地 probe 通过。
- `AWAITING_HUMAN` 对 AUDITOR 开放：`tasks.py:831-837` 只限制 `ESCALATED_TO_HUMAN`。
- `reviewed_by` 可由 workspace key 的 `agent` 字段伪造：`tasks.py:829`；`auth.py:400-412`。
- workspace key approve 时可自动 verify artifact：`tasks.py:860-872`；`documents.py:1137-1183`。

**最小修复：** 对两个 human 状态统一 `principal.is_human`；非人类拒绝 `next_status=DONE`；`human_feedback` 只允许人类；approve 的 verify 仅由人类主体触发；审计记录 `auth_kind/token_id/user_id`。

### H3 VIEWER 改污点（High，已复现）

- `POST /v1/human/documents/{doc_key}/taint` 使用 `get_human_principal`（`human_routes.py:254-267`），`set_document_taint` 只检查 `principal.is_human`（`documents.py:1411-1418`）。
- VIEWER 的 role 是 `RoleScope.VIEWER`（`auth.py:170-174`），没有 `doc:verify`/`doc:write`，但该服务不检查权限。
- `clear_document_taint` 会把 `tainted=False`、`trust_level="TRUSTED"` 并删除 taint edges（`services/taint.py:434-456`）。

**最小修复：** 该路由改用 `get_human_writer` 或显式 `require_permission(principal, "doc:verify")`；为 taint 变化增加单独权限；VIEWER 一律拒绝。

### H4 SSRF/外部动作（High）

- `app/services/outbound.py:41-53`：`_is_private` 漏掉 CGNAT；实测 `100.64.0.1`、`100.100.100.200` 均 `is_private=False`。
- `outbound.py:56-89` 解析 DNS，`outbound.py:134-140` 的 httpx 再解析一次；无 IP pinning，TOCTOU。
- `webhooks.py:158-174`（test）和 `274-302`（delivery）没有调用 `validate_target`；只在 create/update 校验。
- `external_actions.py:355-362` 先校验再发送；同样 TOCTOU。
- `action_out` 不返回 `payload/headers`（`external_actions.py:31-54`；`schemas.py:1418-1439`），但返回 `compensation_payload`；审批者看不到真正要发送的 body。
- `external_actions.py:236-256, 304-326` 补偿动作直接 `status="APPROVED"`，undo 时自动执行。
- `outbound.py:146` 使用 `response.text` 后再截断，响应体无上限；单 dispatcher 串行，慢响应可拖死所有租户投递。

**最小修复：** 在 `send_json` 内校验并 pin IP；每次 webhook 发送都重校验；拒绝 `not ip.is_global`；审批详情返回脱敏 payload/headers/摘要；补偿重新审批；流式读取带硬字节上限和总 deadline；按租户隔离 worker。

### H5 账号预劫持（High，Google 开启后）

- `accounts.py:265-287` 注册不验证邮箱即签发 session/创建 workspace。
- `accounts.py:336-347` Google 登录按 email 找已有用户；`google_sub` 未存储/未校验（`v3_2_production.sql:147-159`）。
- `accounts.py:235-239` 重复邮箱返回 409，形成全局账号枚举。
- `accounts.py:115-141` 临时密码无强制修改；`human_routes.py:531-548` 仅 OWNER 能改他人密码；没有自助重置。

**最小修复：** 邮箱验证 + `google_sub`；不自动绑定未验证账号；全局/每租户邮箱唯一策略与邀请机制；统一重复注册响应；重置令牌 + forced password change。

### H6 未认证资源耗尽（High）

- `accounts.py:44-50` 同步 scrypt；`accounts.py:275-287` 限流后 hash；`database.py:51-58` 异常 rollback。
- 重复邮箱 409 时计数回滚；本地代码路径已确认。
- `config.py:105-106` 默认开放注册；无全局 IP/子网配额。
- `workspace.py:481-521` 可无限创建 agent token；`schemas.py:510` 每 token 1,000,000/min。
- 公开 `/v1/auth/login` 和 `/v1/auth/register` 是攻击面；当前 Caddy 会覆盖客户端 XFF，公网不能直接伪造 IP，但直接接入 `web` 网络的容器可以。

**最小修复：** `anyio.to_thread` + 并发信号量；限流计数独立 commit；先查邮箱再 hash；注册验证/CAPTCHA/配额；token 数量上限；全局 auth 限流。

### H7 嵌入成本/连接池（High）

- `documents.py:525/683` 每次写调用 `_embed`；`retrieval.py:19-60` 每次重嵌全部 chunks。
- `human_routes.py:701-712` 任意 human_writer（OWNER/REVIEWER）可无限 reindex，最多 500 docs。
- `documents.py` 没有 `assert_budget`；只有 timeline/external action 调用。
- `database.py` pool 10+20；embedding 网络/重试发生在事务内；30 个并发即可占满。
- `config.py:126` 生产关闭私网，但 embedding provider 是外部 Jina。

**最小修复：** 文档/embedding 前调用硬预算；按字节/调用次数计费；把 embedding 移出事务或使用独立 worker；每租户并发上限；reindex 加冷却/审批。

### H8 REVIEWER 过权 + Webhook 历史重放（High/Medium）

- `auth.py:170-174`：只有 VIEWER 降级，REVIEWER/OWNER 都是 COMMANDER。
- `schemas.py:1162-1166`：新人类用户默认 REVIEWER。
- `human_routes.py` 中 webhook/policy/budget/undo/action approve 都使用 `get_human_writer`，REVIEWER 全部可用。
- `webhooks.py:237-269`：新规则 `last_event_id=0`，会从最老的 500 个事件开始投递，直到追上；`models.py:1201-1203` 默认 0。
- `webhooks.py:274-302` 不检查 `rule.active`；停用规则后队列仍会继续发送。

**攻击：** 被邀请的 REVIEWER（或被盗的 REVIEWER 会话）创建 Webhook 指向攻击者域名，立即收到工作区历史事件元数据，之后持续接收新事件；还可修改预算、停用 proposal-required policy、批准外部动作。UI 对 REVIEWER 的描述是“可审批”，并未提示这些管理权限。

**修复：** 为 OWNER/REVIEWER/VIEWER 建立显式权限矩阵；REVIEWER 只保留审批/读取；Webhook/policy/budget/agent-token 仅 OWNER；新 Webhook 的 `last_event_id` 初始化为当前最大事件 ID；delivery 检查 active。

### M1 运营令牌存储与浏览器安全（Medium）

- `dashboard/app/page.tsx:69-70,192-193,443-444`：`agentgraph.admin_token` 和 `agentgraph.session_token` 都在 localStorage；`connect/page.tsx:49-51` 写入 admin token；`login/page.tsx:199-200` 在客户登录页直接暴露 “Operator access” 链接。
- 生产响应头经 curl 核验：无 `Content-Security-Policy`、`X-Frame-Options`、`Strict-Transport-Security`、`X-Content-Type-Options`、`Referrer-Policy`；`Server: uvicorn`。
- 未发现当前可利用的存储型 XSS：`dashboard/app/components/markdown.tsx` 在 `rehype-raw` 后跑 `rehype-sanitize`，没有 `dangerouslySetInnerHTML`/`innerHTML`/`eval`。但一旦未来出现同源脚本或恶意扩展，全局 ADMIN_TOKEN 会被直接读走；当前静态 admin token 无服务端过期/撤销。
- 第三方 GIS/Swagger CDN 脚本与本地存储同源，供应链面也需要 CSP 限制。

**修复：** 运营端改用 HttpOnly/Secure/SameSite 的短期服务端会话或 BFF；至少把 admin token 放 `sessionStorage`/内存；移除登录页的 Operator 链接；加 CSP、X-Frame-Options、HSTS、nosniff、Referrer-Policy；运营端独立子域或 IP 白名单。

### M2 RLS fail-open（Medium）

- `tenant.py:38-57`：`SET LOCAL ROLE`/`set_config` 失败只 warning，然后继续；`after_begin` 同样。
- `config.py:112` `rls_require_context=False` 且全项目未使用。
- `docker-compose.prod.yml:38` 用 `POSTGRES_USER`（默认 `agentgraph`）连接；生产确认 `agentgraph` 是 superuser，`agentgraph_app` 是 NOLOGIN 且没有显式 membership grant。
- 生产当前没有看到 `tenant RLS activation failed` 日志，说明 RLS 现在应已生效；但任何角色/权限配置变化都会让它静默退化成“只靠应用层 workspace_id 过滤”。
- 所有策略还接受 `current_setting('app.bypass_rls')='on'`（`v3_2_production.sql:578-591`），一旦存在 SQL 注入原语就是跨租户开关。

**修复：** 激活失败必须抛 503，不能继续；连接角色改为非 superuser + 显式 membership；移除 bypass GUC；增加应用启动自检和真实非超级用户集成测试。

### M3 草稿/污点可见性不是 ACL（Medium）

- `list_documents` 和 `workspace_map` 不按 verification/taint 过滤（`documents.py:298-348`，`workspace.py:119-149`）；`query_documents` 默认隐藏别人草稿/tainted（`documents.py:888-896`），但 census 会暴露所有 doc_key/title/frontmatter。
- `get_document`/`dereference`/版本读取只检查 `doc:read` 和 namespace（`documents.py:478-484`, `961-968`, `1389-1408`），拿到 key 就能读正文和全部历史版本。
- 产品文案说“其他 Agent 未验证草稿默认不可见”，实际只是 discovery 过滤，不是 ACL；新 census 让隐藏内容更容易被发现。

**修复：** 明确“草稿/归档是可见性过滤还是安全边界”；如果是安全边界，在 list/map/direct read/version 统一执行；如果不是，修改文档措辞并提供 `include_unverified/include_tainted` 显式开关。

### M4 Archive 绕过人类锁（Medium）

- `_set_document_archived`（`documents.py:355-405`）只做 namespace、creator/commander、version 检查，不调用 `ensure_not_human_locked`；普通 update/patch 在 `612/630/764` 行有该检查。
- 创建者 Agent 可以 archive 被人类锁定的文档；默认 list/query/map 不再显示，控制台 admin list 默认也不请求 archived。
- 仍可通过 include_archived/直接 dereference 恢复，但普通人类界面会“消失”。

**修复：** archive/restore 同样调用 `ensure_not_human_locked`；锁定文档只允许人类/COMMANDER；给 archive/restore 加频率限制。

### M5 审计身份伪造（Medium）

- `auth.py:118-123`：workspace key 的 `agent_signature` 来自 `X-Agent-Signature`；`effective_agent` 允许 workspace key 用 `agent` 字段任意指定 actor。
- `schemas.py:504-505` 的 agent_signature 是自由字符串；审计只存字符串（`common.py:318-343`），不存 auth_kind/token_id/user_id。
- 影响：可把自动化操作写成 `human_commander`/任意邮箱/其他 agent；`reviewed_by`、`created_by`、timeline actor 都可能失真。

**修复：** 审计记录 `auth_kind/token_id/user_id`；workspace key 保留真实 key 标识；拒绝保留名称/与人类邮箱冲突的签名；`reviewed_by` 只由人类会话写。

### M6 MCP/SSE 绕过限流（Medium）

- `auth.py:209-212` 是 REST 的唯一 `enforce_rate_limit` 调用；`mcp_server/server.py:218-241` 没有调用；`stream_routes.py:83-95` 也没有。
- SSE 没有连接数、每 IP 并发、最大时长；每个订阅者 1000 槽队列；事件总线向所有 workspace 的所有订阅者扇出，过滤在入队后。
- `usage.py:71-74` 的 bucket 用 `workspace:agent_signature:token_id`，workspace key 可改 `X-Agent-Signature` 获得新 bucket；人类每次登录也产生新 token_id。

**修复：** 在 MCP/SSE 认证入口调用同一 limiter；workspace 级总桶 + 每用户/IP 桶；SSE 连接上限和最长生命周期；按 workspace 分片订阅队列。

### M7 大响应/保留策略（Medium）

- `max_page_size=500`（`config.py:79`）；每个文档 summary 包含最多 100KB frontmatter（`max_state_bytes=100_000`）。
- `list_documents` 最多 500 条；`workspace_map` 300 docs + 300 timeline（timeline payload 64KB）；`list_audit` 最多 500 个完整 before/after；`/v1/timeline` 500 条 64KB payload。
- 单次响应可达数十 MB；控制台每次 SSE 事件刷新 14 个端点，包括 audit/documents/slots，放大更明显。
- `timeline_streams`/`state_audit_diffs`/`document_versions`/`rate_limit_windows` 没有清理任务；reaper 只清 `workspace_events`。

**修复：** 列表只返回摘要（截断 frontmatter）；audit 只返回 diff hash/字段级摘要；加聚合响应字节上限；分页 version/audit；加 TTL/保留策略；控制台增量刷新。

### M8 账号生命周期（Medium）

- 只有 `/register`、`/google`、`/login`、`/me`、`/logout`；没有自助改密/重置/邮箱验证。
- `HumanUserUpdateRequest` 允许 OWNER 直接改密码，无需当前密码，也没有 forced-change/通知；临时密码永久有效。
- 登录 timing：未知用户跳过 scrypt，已知用户执行 scrypt；注册 409 可枚举邮箱。
- 没有账号锁定；限流按 email+IP，分布式/多 IP 可绕过。

**修复：** 增加一次性重置令牌、当前密码改密、强制临时密码修改、登录/重置审计与通知；未知用户使用固定 dummy hash；统一错误响应；账号锁定/退避。

### L1-L4 低危/配置项

- **L1**：`.env.example:9` `DOCS_REQUIRE_AUTH=false`、`:97` `CORS_ORIGINS=["*"]`、`:23` `ADMIN_TOKEN=change-me-in-production`。生产实际是 `DOCS_REQUIRE_AUTH=true`、CORS 白名单、ADMIN_TOKEN 64 hex；但按 README 复制 `.env.example` 的自托管部署会不安全。生产 docs 已实测 401。
- **L2**：生产无 HSTS/CSP/X-Frame/nosniff/Referrer-Policy；Caddyfile 只有 `encode` + `reverse_proxy`。
- **L3**：Markdown 允许外部 http(s) 图片/链接；Agent 写文档可埋追踪像素/钓鱼链接；没有脚本执行。
- **L4**：`backups/` 不在 `.dockerignore`（虽未被 Dockerfile COPY，仍进入 build context）；基础镜像未固定 digest；`npm ci --no-audit`；`numpy` 用范围；Next/PostCSS 有 audit 告警，但当前静态导出不运行 Next server/Server Actions/Image API，实际可利用性低，仍建议升级。

---

## 4. 已验证的安全控制

这些不是问题，审计中确认有效：

- 生产 `ADMIN_TOKEN` 为 64 hex（未输出值）；`/docs`、`/openapi.json` 返回 401；CORS 为域名白名单；MCP 有 host/origin 白名单；`OUTBOUND_ALLOW_PRIVATE_NETWORKS=false`。
- 未发现可直达的 SQL 注入：新 frontmatter 过滤使用 SQLAlchemy 绑定参数；RLS role 名有正则校验；递归 CTE 使用参数。
- 未发现当前可利用的存储型 XSS：Markdown 经过 `rehype-raw` + `rehype-sanitize`，未发现 `dangerouslySetInnerHTML`/`innerHTML`/`eval`/`new Function`；`javascript:`/`data:`/事件属性被过滤。
- 未发现 open redirect；根路径固定跳 `/dashboard/`。
- 未发现跨 workspace 的应用层 IDOR：抽查的 `_get_document/_get_task/_get_slot/_get_proposal/_get_action` 均带 `workspace_id`；跨租户的实际风险是 M2 的 RLS fail-open，而不是当前路由。
- 未发现 tracked secrets：git 历史 3 个提交没有 `.env`、真实 token 或数据库 dump；`backups/` 被 gitignore。
- `pip-audit -r requirements.txt` 未发现已知 Python 漏洞。
- 容器以非 root `app` 用户运行；生产 DB 不发布宿主端口。
- SSE 没有把 token 放进 URL；`Last-Event-ID` 不能跨 workspace；问题在 namespace/表级授权，不是跨租户流。

---

## 5. 修复优先级

### P0（立即，1-2 天内）

1. **关闭 namespace 泄漏**：proposals/audit/timeline/task context/links/SSE/external actions。先加服务层统一检查，再补回归测试（至少复现 4 条本地探针）。
2. **收紧人类门禁**：`ESCALATED_TO_HUMAN`/`AWAITING_HUMAN` 必须 `is_human`；非人类禁止 handoff `DONE`、禁止写 `human_feedback`；workspace key 不再能处理升级任务。
3. **VIEWER taint 路由改为 writer/verify 权限**。
4. **外部动作审批可见 payload/headers + 补偿重新审批**；Webhook 每次发送重校验 + DNS/IP pinning。
5. **注册 DoS 修复**：hash 移线程池、限流独立 commit、先查邮箱、加全局配额；文档 embedding 接入硬预算和并发上限。

### P1（一周内）

6. 账号体系：邮箱验证、`google_sub`、重置/改密、统一枚举响应。
7. REVIEWER 权限矩阵；Webhook `last_event_id` 初始化当前最大 ID；delivery 检查 active。
8. 运营端 token 从 localStorage 移到短期 HttpOnly 会话/BFF；移除登录页 Operator 链接；加 CSP/HSTS/X-Frame/nosniff/Referrer-Policy。
9. RLS 改为 fail-closed，连接使用非超级用户；移除 `app.bypass_rls`。
10. 草稿/污点可见性统一（list/map/direct read/version），或修改产品文案并加显式开关。
11. Archive/restore 加 human lock 检查；审计记录 auth_kind/token_id/user_id。

### P2（加固/持续）

12. 大响应摘要化、分页、聚合字节上限、审计/版本/时间线/限流表 TTL。
13. MCP/SSE 限流；SSE 连接数与时长上限；per-workspace 队列。
14. `/readyz` 缓存/限流；请求体总大小上限；slot patch 后大小校验。
15. 依赖升级（Next/PostCSS 在维护窗口；确认静态导出不受影响）、基础镜像 digest、依赖 hash、CI 安全扫描；`.dockerignore` 排除 backups。
16. 管理员/文档 Basic 认证限流；生产启动时拒绝 placeholder/低熵 ADMIN_TOKEN。

---

## 6. 审计限制与说明

- 没有对生产做写操作、暴力破解、拒绝服务或 SSRF 实测；生产只做了只读响应头、认证状态和配置核验。
- 本地探针使用一次性 pgvector + uvicorn，验证了 namespace 四链路、VIEWER taint、workspace key 处理升级任务、Agent self-DONE/human_feedback；探针文件已删除，未改动项目代码。
- Google 登录当前在生产关闭，H5 的 Google 绑定部分是“启用后可用”的代码级结论。
- 公网 `X-Forwarded-For` 伪造未列为可利用：Caddy 默认忽略客户端提供的 XFF 并覆盖；但 API 还在共享 `web` 网络上，其他容器可直接访问 `agentgraph-api:8000` 并伪造 XFF，属于环境风险（M6 的限流绕过前提之一）。
- `npm audit` 因镜像端点不可用，改用官方 registry 后报告 Next/PostCSS 告警；当前是 `output: "export"` 静态站点，没有 Next server/Server Actions/Image Optimization 暴露面，因此未将其列为生产 High。
- 本次未做二进制/容器逃逸、PostgreSQL 内核、Caddy 配置漏洞的深度审计；Caddyfile 只在生产上只读查看。

---

## 7. 修复状态（2026-10-20 第二轮）

已在 `sql/migrations/v3_4_security_hardening.sql` 和代码中完成以下修复，并新增
`tests/test_security_hardening.py` 回归测试：

| 项目 | 状态 | 说明 |
|---|---|---|
| H1 namespace 隔离 | 已修复 | 提案增加 namespace 列；审计在受限 token 下拒绝；timeline/task context/links/SSE/actions 统一过滤；本地探针由“可读”变为拒绝 |
| H2 人类门禁 | 已修复 | `ESCALATED_TO_HUMAN`/`AWAITING_HUMAN` 必须 `is_human`；Agent 禁止 handoff DONE 和写 `human_feedback`；reviewed_by 使用认证主体 |
| H3 VIEWER taint | 已修复 | human taint 路由要求 writer/verify；VIEWER 返回 403 |
| H4 SSRF/审批 | 已修复 | 发送时重新解析并固定 IP；拒绝非 global/CGNAT；webhook test/delivery 每次重校验；响应体上限和硬超时；审批列表返回 payload/headers；补偿改为 PENDING_APPROVAL |
| H5 Google 预劫持 | 部分修复 | 新增 `google_sub`/`email_verified`，Google 不再按 email 自动绑定本地密码账号，新增 `/v1/auth/google/link`；邮箱验证和忘记密码仍依赖 SMTP，未在本轮接入 |
| H6 注册/scrypt DoS | 已修复 | scrypt 移入有界线程池；限流计数独立提交；按 IP/全局/邮箱三重桶；重复邮箱在 hash 前拒绝 |
| H7 embedding 成本 | 已缓解 | 每工作区每分钟 embedding 字节预算；批量/重索引/写入统一计费；失败回滚仍保留成本计数 |
| H8 REVIEWER/Webhook | 已修复 | 新用户默认 VIEWER；预算/策略/Webhook/reindex 仅 OWNER；新 Webhook 从当前事件 ID 开始；停用规则取消队列 |
| M1 浏览器令牌 | 已修复/缓解 | Operator token 改为 sessionStorage；登录页移除 Operator 链接；新增 CSP/HSTS/X-Frame/nosniff/Referrer-Policy；human session 仍在 localStorage（有 CSP，但非 HttpOnly） |
| M2 RLS | 已修复/部分 | RLS 激活失败改为 503；删除 `app.bypass_rls` 策略分支；应用仍用 PostgreSQL 超级用户连接，非超级用户运行时连接仍需部署层改造 |
| M3 草稿/污点可见性 | 已修复 | `list_documents`/`workspace_map`/direct get/dereference/version 增加显式 `include_unverified`/`include_tainted`，受限 Agent 默认隐藏他人未验证/tainted 内容 |
| M4 Archive 人类锁 | 已修复 | archive/restore 调用 `ensure_not_human_locked` |
| M5 审计身份 | 已修复/缓解 | 审计行新增 `actor_auth_kind`/`actor_token_id`/`actor_user_id`；reviewed_by 不再接受请求体伪造 |
| M6 MCP/SSE 限流 | 已修复 | MCP 和 SSE 接入同一限流；SSE 增加全局/每主体连接上限 |
| M7 大响应/保留 | 部分修复 | list/map frontmatter 超限压缩；audit/timeline 的完整内容响应和长期保留仍需进一步摘要化/TTL |
| M8 账号生命周期 | 部分修复 | 新增自助改密 `/v1/auth/password`（撤销其他会话）；邮箱验证、忘记密码邮件仍缺 SMTP |
| L1-L4 | 已修复/缓解 | `.env.example` 安全默认、生产强 ADMIN_TOKEN 校验、构建上下文排除 backups、numpy 固定、Markdown 外部图片不自动加载、外链 no-referrer |

**仍公开标注的残余风险：** 邮件验证/忘记密码（需要 SMTP 供应商）、应用数据库账号仍为超级用户、审计/时间线大响应与保留策略、依赖升级（Next/PostCSS 静态导出下不可直接利用）、共享 `web` 网络内的邻居容器。

**验证：** 本地完整测试 `99 passed`；新增安全回归覆盖 namespace、VIEWER taint、workspace key 人类门禁、Agent self-DONE/human_feedback、REVIEWER 预算/Webhook 权限。生产已应用 `schema_migrations 3.4.0`，`/readyz` 正常，REST 八项 smoke、MCP 22 工具/游标 smoke 全部通过；生产响应已带 CSP/HSTS/X-Frame/nosniff/Referrer-Policy。剩余项见上表“部分修复”和“仍公开标注的残余风险”。
