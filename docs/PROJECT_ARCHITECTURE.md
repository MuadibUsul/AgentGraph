# AgentGraph 项目介绍、架构与路径

| 项 | 值 |
|---|---|
| 产品版本 | **v3.2.1** |
| 数据库 schema | **3.26.0** |
| 文档更新时间 | **2026-10-23** |
| 本地仓库 | `E:\Codes\AgentGraph` |
| 生产地址 | `https://veynod.com`（旧域名 `agentgraph.fusionpropulsion.space` 保留为备用） |
| 远程仓库 | `MuadibUsul/AgentGraph`（private，`main`） |

---

## 1. 项目介绍

### 1.1 一句话

**AgentGraph 是一个面向多 Agent 团队的共享黑板（shared blackboard）和人机协作控制台。**

Agent 通过 REST 或 MCP 领取任务、读写共享状态与文档；人类通过 Web 控制台审批、修正、冻结关键状态、查看审计并撤销 Agent 的整批操作。它不是“再做一个聊天界面”，而是把多 Agent 协作中最容易失控的几件事做成存储层和服务层能力：

- 上下文如何分层传递，而不是把所有日志塞给下游；
- 多个 Agent 如何原子领取任务，崩溃后如何自动回收；
- 人类修正过的状态如何不被 Agent 覆盖；
- Agent 之间的反复驳回如何熔断；
- 哪些内容可信，哪些内容来自未验证来源；
- 每一次写操作如何审计、回滚和解释；
- 多租户、命名空间、令牌和能力边界如何隔离；
- 成本、并发、外部副作用如何受控。

### 1.2 三类使用者

| 使用者 | 入口 | 主要动作 |
|---|---|---|
| **Agent** | REST `/v1/*` 或 MCP `/mcp/` | 首次 `get_orientation`；领取任务；读写状态槽；读写文档；提交 handoff；提交提案；请求人类审核 |
| **人类成员** | `/dashboard/login/` → `/dashboard/` | 审批/驳回任务；编辑和验证文档；人类锁定；查看审计；撤销 Agent 会话批次；管理提案、Webhook、预算和账号 |
| **部署运维者** | `/dashboard/connect/` | 用服务器 `ADMIN_TOKEN` 进入 operator 控制面；不面向普通客户展示 |

### 1.3 要解决的问题

| 问题 | AgentGraph 的机制 |
|---|---|
| **上下文爆炸** | L0 工作区地图 → L1 结构化 handoff brief → L2 文档 artifact，按需 `dereference` |
| **并发覆盖 / 死锁** | 任务与状态槽 TTL 租约、心跳、数据库时钟、fencing token、后台 reaper |
| **Agent 无限互相驳回** | `ping_pong_count` 达到阈值后强制 `ESCALATED_TO_HUMAN` |
| **人类指令被覆盖** | `locked_by_human` 物理拦截 Agent 写入；人类覆盖产生 `HUMAN_OVERRIDE` 事件 |
| **不可信内容 / 提示注入** | `trust_level`、不可信来源包裹、传递式污点 `taint_edges`、Agent 验证被阻止 |
| **错误写入无法追责** | 每次写入记录 before/after 镜像；按 `session_batch_id` 冲突感知回滚 |
| **文档被悄悄改坏** | 不可变 `document_versions` + SHA-256；验证绑定精确版本与哈希 |
| **Agent 权限过大** | `SCOUT / ANALYST / AUDITOR / EXECUTOR` 角色令牌 + namespace 沙盒 |
| **租户之间互相看见** | 租户树 + PostgreSQL RLS + 由触发器推导的 `tenant_id` |
| **外部副作用失控** | 外部动作必须人工批准；Webhook 持久化、签名、重试；补偿动作单独审批 |
| **成本与滥用** | 预算、用量账本、固定窗口限流、embedding 配额、并发限制 |
| **轮询和断线丢事件** | PostgreSQL outbox + `LISTEN/NOTIFY` + 可重放 SSE |

### 1.4 产品边界

AgentGraph 目前**不是**：

- 通用聊天机器人或 Agent 编排框架；
- 只会做向量检索的“记忆数据库”；
- 只服务单用户的笔记工具；
- 自动替人类做审批的系统。人类门禁是产品语义的一部分，不是可选装饰。

控制台是**operator surface**，不是营销页。所有统计都来自真实 API/数据库，不预置演示业务数据。

---

## 2. 总体架构

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                              使用者与客户端                                  │
│                                                                              │
│  Agent runtime                Human browser              Deployment operator │
│  MCP client / REST client     /dashboard/login/          /dashboard/connect/ │
└───────────────┬───────────────────────┬──────────────────────────┬───────────┘
                │ HTTPS                 │ HTTPS                    │ HTTPS
                ▼                       ▼                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Caddy / TLS / 反向代理                               │
│              veynod.com → agentgraph-api:8000                              │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         AgentGraph Gateway (FastAPI)                         │
│                                                                              │
│  Middleware                                                                  │
│  ─ RequestContext / X-Request-ID   ─ Protocol / AgentGraph-Version           │
│  ─ CORS / Security headers         ─ Per-token concurrency limit             │
│                                                                              │
│  HTTP surfaces                                                               │
│  ─ /v1/*              Agent + commander REST                                │
│  ─ /v1/auth/*         Human email/Google auth                               │
│  ─ /v1/human/*        Human console API                                     │
│  ─ /v1/admin/*        Operator API (ADMIN_TOKEN)                            │
│  ─ /v1/stream         Replayable SSE                                        │
│  ─ /mcp/              MCP streamable HTTP                                   │
│  ─ /dashboard/        Next.js static export                                 │
│                                                                              │
│  Service layer (REST / MCP / console share the same code)                    │
│  ─ orientation / workspace / tasks / leases / slots / documents             │
│  ─ proposals / policies / taint / retrieval / onboarding / lineage          │
│  ─ accounts / budgets / exports / webhooks / external actions               │
│                                                                              │
│  Background loops                                                            │
│  ─ lease reaper  ─ webhook dispatcher  ─ external-action dispatcher          │
│  ─ async index worker                                                        │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ Async SQLAlchemy + asyncpg
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         PostgreSQL 16 + pgvector                             │
│                                                                              │
│  Tenant / identity      Collaboration        Documents / retrieval           │
│  ─ tenants              ─ collab_tasks       ─ shared_documents              │
│  ─ workspaces           ─ leases             ─ document_versions             │
│  ─ memberships          ─ context_slots      ─ document_chunks               │
│  ─ agent_tokens         ─ workspace_links    ─ document_index               │
│  ─ human_users          ─ timeline_streams   ─ proposals / votes             │
│  ─ human_sessions       ─ state_audit_diffs  ─ path_policies / taint_edges   │
│  ─ grant_snapshots                           ─ namespaces / aliases          │
│                                                                              │
│  Governance / ops       Realtime                                             │
│  ─ workspace_budgets    ─ workspace_events (outbox)                          │
│  ─ usage_ledger         ─ triggers → pg_notify → SSE                         │
│  ─ rate_limit_windows                                                        │
│  ─ webhook_rules / deliveries                                                │
│  ─ external_actions / attempts                                               │
│                                                                              │
│  RLS: app.workspace_id + app.tenant_scope, fail-closed                       │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              外部依赖                                        │
│  Jina / OpenAI-compatible embeddings · Google Identity · Webhook targets     │
│  Approved external actions · SMTP/email verification (not implemented yet)   │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 关键架构选择

1. **单 PostgreSQL 核心**：任务、状态、文档、审计、事件、预算、Webhook 和租约全部在 PostgreSQL 内完成；不引入 Redis、Kafka 或外部任务队列。

2. **一套服务层，三个入口**：REST、MCP 和 Web 控制台共用 `app/services/`，避免三套业务逻辑漂移。

3. **数据库时钟优先**：租约、fencing、限流和事件顺序使用数据库 `NOW()`/序列，而不是各进程的本地时间。

4. **审计即产品能力**：每次 Agent 写入都有 before/after image；回滚是有版本控制的恢复，不是日志重放。

5. **人类主权不可绕过**：即使 Agent 拥有写权限，也不能覆盖 `locked_by_human = true` 的资源。

6. **失败关闭（fail-closed）**：RLS 上下文缺失、生产 `ADMIN_TOKEN` 过弱、租户上下文无法建立时，请求失败而不是绕过隔离。

7. **所有对象都有地址**：规范 ID（`ws_`、`doc_`、`task_`、`slot_`、`tok_`、`prn_`）+ 可读别名；重命名保留旧别名重定向，删除保留 tombstone。

---

## 3. 代码分层

### 3.1 请求生命周期

```text
HTTP / MCP request
  → CORS / security headers
  → ProtocolMiddleware（AgentGraph-Version、错误协议）
  → RequestContextMiddleware（X-Request-ID、日志）
  → ConcurrencyLimitMiddleware（按令牌的在途请求限制）
  → 路由层（v1 / auth / human / admin / stream / MCP）
  → 认证与 Principal 解析（workspace / agent / human / human_admin）
  → tenant RLS 上下文（SET LOCAL ROLE + app.workspace_id）
  → 领域服务（app/services/*）
  → 引擎层（patch / merge / chunking / formatter / embeddings）
  → SQLAlchemy ORM / asyncpg
  → 事务提交 + audit + timeline + outbox → NOTIFY
```

### 3.2 入口与基础设施

| 文件 | 职责 |
|---|---|
| `app/main.py` | 创建 FastAPI；lifespan 中执行 schema、启动 reaper/webhook/action/index 循环和 MCP session manager；挂载 REST、admin、SSE、`/mcp`、`/dashboard`；统一异常格式、健康检查、安全响应头 |
| `app/config.py` | Pydantic Settings：版本、数据库、租约、配额、SSE、MCP、embedding、RLS、CORS、生产开关 |
| `app/database.py` | async engine/session；advisory lock 下的 schema bootstrap；`sql/init.sql` + 增量迁移链；v2→v3 兼容迁移；向量维度检查 |
| `app/auth.py` | API Key 哈希、Agent Token 生成；把 Bearer 解析为 `Principal`；RBAC、namespace、人类锁、租户上下文 |
| `app/models.py` | 全部 SQLAlchemy ORM 表、枚举、关系 |
| `app/schemas.py` | 请求/响应 Pydantic 模型、handoff 契约、租约/导出/账号等 schema |
| `app/errors.py` | 统一错误对象 `{code, http_status, message, details, ...}` |
| `app/error_registry.py` | 错误码注册表，生成 `docs/ERROR_CODES.md` |
| `app/middleware.py` | 纯 ASGI 请求日志、`X-Request-ID`，不阻断 SSE |
| `app/protocol_middleware.py` | API 版本协商、错误协议、响应头 |
| `app/concurrency.py` | 按 principal/token 的在途请求并发限制 |
| `app/events.py` | 每进程单 LISTEN 连接 → SSE 订阅队列扇出 |
| `app/tenant.py` | 事务内 `SET LOCAL ROLE`、`app.workspace_id`、`app.tenant_scope` |
| `app/addresses.py` | 规范 ID、别名解析、`doc://`、`$ref`、namespace 地址语法 |
| `app/versioning.py` | API 版本语义与兼容层 |
| `app/ids.py` | ULID/规范 ID 生成辅助 |
| `app/stamp_context.py` | 请求级 lineage stamp 上下文 |
| `app/warnings_context.py` | 请求级警告收集（legacy alias、自动注册等） |
| `app/agent_guide.py` | 生成 Agent 可读连接指南 |
| `app/cli.py` | `init-db`、workspace、agent-token、stats 等 CLI |

### 3.3 服务层 `app/services/`

REST、MCP、admin 和 human console 共用这一层。

| 文件 | 职责 |
|---|---|
| `accounts.py` | 人类账号、密码哈希、会话、Google 身份绑定 |
| `addresses.py` | 规范 ID、别名重定向、命名空间注册、文档 parts、重命名/清理 |
| `audit.py` | 会话批次查询、before/after diff、冲突感知 undo |
| `budget.py` | embedding 计量、令牌写入限制、循环熔断 |
| `common.py` | 序列化、审计写入、事件写入、快照、数据库时钟 |
| `documents.py` | 文档创建/更新、不可变版本、验证、信任、人类锁、L2 artifact、section patch、语义查询 |
| `attestation.py` | 跨租户可验签谱系：租户 Ed25519 密钥与轮换、JWKS、成对假名、紧凑 JWS、实时吊销查询 |
| `export_schedule.py` | 定期导出：每周全量、每日增量、30 天保留 |
| `exports.py` | 租户/工作区导出、导入、往返一致性 |
| `external_actions.py` | 审批式外部动作、幂等、尝试账本、补偿 |
| `idempotency.py` | REST/MCP 写请求的幂等记录与 24h 重放 |
| `indexer.py` | 异步文档索引 worker、状态、重试、滞后统计 |
| `leases.py` | 统一租约：心跳、fencing、过期、孤儿、交接（含稀缺资源租约） |
| `lifecycle.py` | 文档生命周期：expires_at/superseded_by、计算出的 lifecycle、2026-11 查询默认排除、supersede |
| `digest.py` | 命名水位线（归属主体）、"离开期间"摘要、orientation 的 since_last_seen |
| `lineage.py` | lineage stamp、grant snapshot、追溯到根令牌 |
| `links.py` | 任务/文档/槽位之间的 typed links、依赖环检测 |
| `onboarding.py` | `system/constitution`、namespace readme、首次接入脚手架 |
| `orientation.py` | `get_orientation` 聚合包：身份、宪法、命名空间、任务、next calls |
| `outbound.py` | Webhook/外部动作的安全 HTTP：DNS 固定、私网阻断、响应上限、超时 |
| `policies.py` | glob 路径策略、角色/内容大小/JSON Schema 门禁 |
| `proposals.py` | 提案创建、Arena 投票、三方 rebase、apply/reject |
| `reaper.py` | 过期任务/槽位/资源租约回收循环 |
| `resources.py` | 稀缺资源：容量槽位租约、FIFO 队列、resource.granted 与 60 秒确认 |
| `reviews.py` | 同行评审：peer_review 任务、reviews/ 评审文档、作者谱系冲突、独立评审封存 |
| `retrieval_eval.py` | 检索评测：评测集、recall@k/MRR 历史、回归门禁（recall@50 下降 ≤ 3 个百分点）、每日 self_recall@50 探针 |
| `retrieval.py` | Markdown 分块、HNSW + tsvector + trigram 混合检索、RRF |
| `slots.py` | 状态网格、RFC 6902 JSON Patch、租约、人类主权 |
| `taint.py` | 来源抽取、污点边、下游文档/槽位/任务传播、人工清除 |
| `tasks.py` | 任务创建、claim/renew/release、handoff、review、ping-pong 熔断、escalate |
| `tenants.py` | 租户树、成员同步、暂停/恢复/删除宽限期 |
| `timeline.py` | 追加/读取 timeline，namespace 与权限检查 |
| `usage.py` | 工作区预算、用量账本、固定窗口限流 |
| `webhooks.py` | 持久化规则、outbox 匹配、HMAC 签名、指数退避 |
| `workspace.py` | 工作区创建、L0 map、统计、Scoped Agent Token 铸造/轮换 |

### 3.4 引擎层 `app/engine/`

| 文件 | 职责 |
|---|---|
| `blackboard.py` | 租约计算、RFC 6902 JSON Patch、Markdown 标题定位与段落替换、L1 brief 校验、`doc://` 解析 |
| `formatter.py` | YAML frontmatter + Markdown；L1 brief、任务卡、状态槽、timeline 的紧凑 Agent 视图 |
| `embeddings.py` | OpenAI-compatible / Ollama / 离线 hash 向量化；Jina v3 生产配置；维度校验与截断 |
| `chunking.py` | 按 Markdown 标题/段落分块，供向量、tsvector、trigram 检索 |
| `merge.py` | 行级三方合并；非重叠编辑合并，重叠冲突返回 409 |
| `notion.py` | Notion/HTML 检测、表格/标题/列表转换、安全过滤 |

### 3.5 API 路由

| 文件 | 前缀 | 说明 |
|---|---|---|
| `app/api/v1_routes.py` | `/v1` | Agent/commander REST：orientation、任务、租约、槽位、文档、提案、查询、审计、导出、命名空间、timeline |
| `app/api/auth_routes.py` | `/v1/auth` | 人类邮箱注册/登录、Google 登录/绑定、改密、登出、公开 auth config |
| `app/api/human_routes.py` | `/v1/human` | 登录后的人类控制台 API：审批、编辑、验证、提案、审计、预算、Webhook、外部动作、账号、租户 |
| `app/api/admin_routes.py` | `/v1/admin` | Operator API：工作区、租约、任务强制操作、密钥轮换、策略、预算、租户、导出/导入 |
| `app/api/stream_routes.py` | `/v1` + `/v1/admin` | 可重放 SSE：Agent/commander 流与人类控制台流 |

### 3.6 后台循环

在 `app/main.py` 的 lifespan 中启动，随应用关闭一起取消：

| 循环 | 作用 |
|---|---|
| `lease_reaper_loop` | 回收过期任务/槽位租约，递增 epoch，防止旧持有者继续写 |
| `webhook_delivery_loop` | 匹配 outbox 事件，签名并投递 Webhook，失败退避重试 |
| `external_action_loop` | 分发已批准的外部动作，记录尝试与补偿 |
| `index_worker_loop` | 异步重建文档 chunks 和向量索引，记录 pending/failed/lag |

---

## 4. 完整目录与路径

```text
E:\Codes\AgentGraph\
├── app/
│   ├── __init__.py
│   ├── main.py                       # FastAPI 入口、lifespan、路由挂载、健康检查
│   ├── config.py                     # 所有环境变量与运行参数
│   ├── database.py                   # async engine/session、schema bootstrap、迁移链
│   ├── auth.py                       # Principal、API Key、RBAC、namespace、人类锁
│   ├── models.py                     # 36 张核心表的 ORM
│   ├── schemas.py                    # 请求/响应 Pydantic 契约
│   ├── errors.py                     # ApiError 与统一错误体
│   ├── error_registry.py             # 错误码注册表
│   ├── middleware.py                 # 请求日志、X-Request-ID
│   ├── protocol_middleware.py        # API 版本与协议错误
│   ├── concurrency.py                # 按令牌并发限制
│   ├── events.py                     # LISTEN/NOTIFY → SSE
│   ├── tenant.py                     # RLS 上下文
│   ├── addresses.py                  # 规范 ID、别名、$ref
│   ├── versioning.py                 # API 版本语义
│   ├── ids.py                        # ULID / canonical ID
│   ├── stamp_context.py              # lineage stamp 上下文
│   ├── warnings_context.py           # 请求级警告
│   ├── agent_guide.py                # Agent 连接指南生成
│   ├── cli.py                        # 初始化/工作区/令牌 CLI
│   │
│   ├── api/
│   │   ├── v1_routes.py              # /v1：Agent + commander REST
│   │   ├── auth_routes.py            # /v1/auth：人类认证
│   │   ├── human_routes.py           # /v1/human：人类控制台
│   │   ├── admin_routes.py           # /v1/admin：operator
│   │   └── stream_routes.py          # /v1/stream、/v1/admin/.../stream
│   │
│   ├── engine/
│   │   ├── blackboard.py             # patch、section patch、租约、L1 校验
│   │   ├── formatter.py              # frontmatter + Agent 视图
│   │   ├── embeddings.py             # embedding provider
│   │   ├── chunking.py               # Markdown 分块
│   │   ├── merge.py                  # 三方合并
│   │   └── notion.py                 # Notion/HTML → Markdown
│   │
│   ├── mcp_server/
│   │   ├── __init__.py
│   │   ├── __main__.py               # python -m app.mcp_server（stdio）
│   │   └── server.py                 # 88 个 MCP 工具 + HTTP/stdio 认证
│   │
│   └── services/
│       ├── __init__.py
│       ├── accounts.py
│       ├── addresses.py
│       ├── audit.py
│       ├── budget.py
│       ├── common.py
│       ├── documents.py
│       ├── exports.py
│       ├── external_actions.py
│       ├── idempotency.py
│       ├── indexer.py
│       ├── leases.py
│       ├── lineage.py
│       ├── links.py
│       ├── onboarding.py
│       ├── orientation.py
│       ├── outbound.py
│       ├── policies.py
│       ├── proposals.py
│       ├── reaper.py
│       ├── retrieval.py
│       ├── slots.py
│       ├── taint.py
│       ├── tasks.py
│       ├── tenants.py
│       ├── timeline.py
│       ├── usage.py
│       ├── webhooks.py
│       └── workspace.py
│
├── sql/
│   ├── init.sql                      # 基础 DDL + outbox 触发器（幂等）
│   └── migrations/
│       ├── v2_to_v3_collab.sql       # v2 → v3 原地迁移
│       ├── v3_hardening.sql          # 3.1：版本/信任/fencing/outbox
│       ├── v3_2_production.sql       # 3.2：提案/策略/污点/RLS/检索/出站
│       ├── v3_2_1_mcp_improvements.sql
│       ├── v3_3_agent_feedback.sql   # 软归档、批量、列举契约
│       ├── v3_4_security_hardening.sql
│       ├── v3_5_artifact_binding.sql
│       ├── v3_6_failure_protocol.sql
│       ├── v3_7_tenants.sql
│       ├── v3_8_addresses.sql
│       ├── v3_9_lineage.sql
│       ├── v3_10_review_rounds.sql
│       ├── v3_11_onboarding.sql
│       ├── v3_12_budget.sql
│       ├── v3_13_async_index.sql
│       ├── v3_14_budget_enforce.sql
│       ├── v3_15_leases.sql
│       ├── v3_16_exports.sql
│       ├── v3_17_events.sql
│       ├── v3_18_onboarding_progress.sql
│       ├── v3_19_agent_onboarding.sql
│       ├── v3_20_durability_incidents.sql
│       ├── v3_21_token_tree.sql
│       ├── v3_22_members.sql
│       ├── v3_23_collaboration.sql
│       ├── v3_24_provenance_recall_budgets.sql
│       ├── v3_25_logs_contention_situation.sql
│       └── v3_26_tenant_tree.sql
│
├── dashboard/
│   ├── package.json                  # Next.js 14.2 / React 18
│   ├── next.config.mjs               # output: export, basePath: /dashboard
│   ├── tsconfig.json
│   ├── out/                          # npm run build 产物，FastAPI 静态托管
│   └── app/
│       ├── layout.tsx                # 根布局
│       ├── page.tsx                  # 控制台外壳、数据加载、SSE、弹窗
│       ├── login/page.tsx            # 客户登录/注册（邮箱、Google）
│       ├── connect/page.tsx          # operator 入口（ADMIN_TOKEN）
│       ├── types.ts                  # 前端类型、时间/状态工具
│       ├── i18n.tsx                  # EN/中文、持久化语言选择
│       ├── globals.css               # 基础样式
│       ├── v3.css                    # 看板/状态/文档/审计/响应式样式
│       ├── design-tokens.css         # Primitive → Semantic → Component tokens
│       └── components/
│           ├── ui.tsx                # Icon、Modal、徽章、Empty、Spinner
│           ├── blackboard.tsx        # 审批队列 + 实时状态网格 + 中断流
│           ├── task-board.tsx        # Kanban、任务详情、审批
│           ├── documents.tsx         # 文档树、验证/信任、版本、Markdown 编辑
│           ├── agents.tsx            # 角色说明、Token 铸造/停用
│           ├── audit.tsx             # 批次、diff、Undo
│           ├── governance.tsx        # 提案、策略、Webhook、外部动作、预算、账号
│           └── markdown.tsx          # GFM + sanitized raw HTML
│
├── tests/
│   ├── conftest.py                   # 真实 uvicorn + pgvector 测试夹具
│   ├── test_production.py            # 生产级端到端与回归
│   ├── test_v3_integration.py        # 任务/槽位/文档/SSE/MCP/租约
│   ├── test_blackboard.py            # patch、section patch、租约、L1
│   ├── test_hardening.py             # 回滚冲突、版本、SSE 重放、fencing
│   ├── test_security_hardening.py    # namespace、人类门禁、SSRF、RLS
│   ├── test_leases.py                # 统一租约生命周期
│   ├── test_feedback.py              # 游标列举、软归档、批量读写
│   ├── test_exports.py               # 导出/导入往返
│   ├── test_tenants.py               # 租户树与删除宽限期
│   ├── test_lineage.py               # 谱系章、grant snapshot
│   ├── test_onboarding.py            # orientation、宪法、namespace readme
│   ├── test_list_contract.py         # 穷举列表契约
│   ├── test_budget.py                # 预算/用量
│   ├── test_budget_enforce.py        # 强制退避/配额
│   ├── test_async_index.py           # 异步索引
│   ├── test_addresses.py             # 规范 ID/别名/墓碑
│   ├── test_failure_modes.py         # 失败协议
│   ├── test_protocol.py              # API 版本/错误协议
│   ├── test_error_registry.py        # 错误码注册表
│   ├── test_notion.py                # Notion/HTML 转换
│   ├── test_merge_properties.py      # 三方合并性质
│   ├── test_schemas.py               # 输入校验
│   ├── test_formatter.py             # Agent 视图
│   ├── test_embeddings.py            # 向量维度/归一化/零填充
│   └── test_phase0_fixes.py          # 早期回归
│
├── scripts/
│   ├── smoke_test.py                 # 部署后端到端冒烟
│   ├── benchmark_retrieval.py        # 真实 recall/latency 基准
│   ├── check_contrast.py             # 设计对比度检查
│   ├── gen_agent_guide.py            # 生成 docs/AGENT_GUIDE.md
│   ├── gen_error_codes.py            # 生成 docs/ERROR_CODES.md
│   └── webhook_receiver.py           # Bot 侧签名 Webhook 接收器
│
├── docs/
│   ├── PROJECT_ARCHITECTURE.md       # 本文档
│   ├── AGENT_GUIDE.md                # Agent 连接/调用指南
│   ├── ERROR_CODES.md                # 错误码注册表
│   ├── PRD_PROGRESS.md               # 需求进度
│   ├── AGENT_FEEDBACK_2026-10-21.md  # Agent 反馈与后续 backlog
│   ├── SECURITY_AUDIT_2026-10-20.md  # 安全审计报告
│   ├── v3-requirements.md            # 合并后的 v3 需求/规格
│   ├── benchmarks/                   # 检索基准结果
│   └── design-system/                # 设计 token、验收、组件与本地设计稿
│
├── Dockerfile                        # 多阶段：Next build + Python runtime
├── docker-compose.yml                # 本地 api + pgvector
├── docker-compose.prod.yml           # 生产 compose（不发布宿主端口）
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml                    # ruff / pytest
├── .env.example                      # 环境变量模板
├── .env                              # 本地敏感配置（gitignore）
├── README.md
└── backups/                          # 本地迁移备份（gitignore）
```

> `dashboard/out` 是 `npm run build` 生成的静态产物；Docker 镜像会内置，FastAPI 在 `/dashboard` 挂载。`node_modules`、`.next`、`.venv`、缓存和 `backups` 不进入版本库。

---

## 5. 数据模型

### 5.1 核心表分组

当前 ORM 中共有 **36 张核心表**。

| 分组 | 表 | 用途 |
|---|---|---|
| **租户与身份** | `tenants`、`workspaces`、`memberships` | 租户树、工作区、成员关系 |
| | `agent_tokens` | Scoped Agent Key；角色、namespace、启停、grant 快照 |
| | `human_users`、`human_sessions` | 人类账号、密码/Google 身份、短时会话 |
| | `grant_snapshots`、`access_audit`、`constitution_acks` | 令牌授权快照、读访问审计、宪法确认 |
| **协作** | `collab_tasks` | 任务状态机、L1 brief、L2 artifact、ping-pong |
| | `leases` | 统一租约：holder、TTL、heartbeat、fencing token |
| | `context_slots` | JSONB 实时状态网格、版本、人类锁 |
| | `workspace_links` | 任务/文档/槽位之间的 typed links |
| | `timeline_streams` | Append-only 时间线 |
| **文档与检索** | `shared_documents` | 文档正文、frontmatter、namespace、验证/信任、软归档 |
| | `document_versions` | 不可变版本、SHA-256、parent、author、batch |
| | `document_chunks` | Markdown 分块、向量、tsvector、trigram |
| | `document_index` | 每篇文档的异步索引状态与滞后 |
| | `document_proposals`、`proposal_votes` | 受审提案、Arena 投票 |
| | `path_policies` | glob 路径策略、角色/大小/JSON Schema 门禁 |
| | `taint_edges` | 来源 → 文档/槽位/任务的污点传播图 |
| | `alias_redirects`、`tombstones` | 重命名别名永久重定向、删除墓碑 |
| | `namespaces`、`document_refs` | 先注册后使用的 namespace、`$ref` 声明 |
| **审计与治理** | `state_audit_diffs` | before/after 镜像、`session_batch_id` |
| | `workspace_events` | 事务性 outbox，单调 id，SSE 重放 |
| | `workspace_budgets`、`usage_ledger`、`rate_limit_windows` | 预算、用量账本、固定窗口限流 |
| | `webhook_rules`、`webhook_deliveries` | 持久化 Webhook 规则、签名投递与重试 |
| | `external_actions`、`external_action_attempts` | 审批式外部动作、尝试账本、补偿 |

### 5.2 关键字段

| 表 | 关键字段 |
|---|---|
| `workspaces` | `id`, `tenant_id`, `name`, `api_key_hash`, `tier`, `canonical_id` |
| `agent_tokens` | `agent_signature`, `role_scope`, `allowed_namespaces`, `api_key_hash`, `is_active`, `parent_token_id` |
| `shared_documents` | `doc_key`, `canonical_id`, `title`, `content`, `content_hash`, `frontmatter`, `verification_status`, `trust_level`, `verified_version`, `verified_hash`, `embedding`, `namespace`, `version`, `locked_by_human`, `archived_at`, `tainted` |
| `context_slots` | `slot_key`, `current_state`, `schema_hint`, `blockers`, `namespace`, `version`, `lease_epoch`, `locked_by`, `lock_expires_at`, `locked_by_human` |
| `collab_tasks` | `status`, `priority`, `assigned_role`, `claimed_by`, `lease_expires_at`, `lease_epoch`, `handoff_brief`, `artifact_uri`, `ping_pong_count`, `max_retries`, `review_feedback`, `reject_history` |
| `leases` | `lease_id`, `resource_kind`, `resource_id`, `holder`, `expires_at`, `ttl_seconds`, `heartbeat_interval_s`, `fencing_token`, `state` |
| `document_versions` | `document_id`, `version`, `parent_version`, `content_hash`, `author`, `session_batch_id` |
| `workspace_events` | 单调 `id`, `source_table`, `action`, `workspace_id`, `payload`, `created_at` |

### 5.3 任务状态机

```text
create ──► READY / TODO
TODO ──依赖全部 DONE──► READY
READY / HANDOFF ──claim──► IN_PROGRESS
IN_PROGRESS ──handoff──► REVIEWING | HANDOFF | DONE | AWAITING_HUMAN | FAILED
REVIEWING ──approve──► DONE
REVIEWING ──reject──► READY（保留 review_feedback / reviewed_by）
ping_pong_count >= max_retries ──► ESCALATED_TO_HUMAN
lease 过期 ──reaper──► READY（lease_epoch + 1，旧持有者写入失效）
人类审批/驳回 ──► DONE / HANDOFF / READY / IN_PROGRESS（重置 PP 计数）
```

`DONE`、`AWAITING_HUMAN`、`ESCALATED_TO_HUMAN` 的最终处置必须由真实人类会话或 operator 完成；Agent 只能 hand off 到 `REVIEWING`/`HANDOFF`。

### 5.4 文档版本与可信状态

```text
文档创建/更新/section patch/人类编辑
  → content_hash = SHA-256(content)
  → INSERT document_versions(version, parent_version, hash, author, batch)
  → 内容变更：verification_status = DRAFT，verified_* = NULL
  → AUDITOR/COMMANDER/人类 verify：
       verified_version = 当前 version
       verified_hash = 当前 hash
       verification_status = VERIFIED，trust_level = TRUSTED
  → 默认 query 只查 VERIFIED + 调用者自己的 DRAFT
  → include_unverified=true 才查其他 Draft
  → SCOUT 来源文档 trust_level=UNTRUSTED，Agent 视图包裹
    [[UNTRUSTED_SOURCE]] … [[/UNTRUSTED_SOURCE]]
```

---

## 6. 核心流程

### 6.1 Agent 首次接入

```text
Agent → GET /v1/orientation（或 MCP get_orientation）
  ← 身份、workspace、role、allowed_namespaces
  ← system/constitution 当前版本
  ← namespace map + readme
  ← collaborators、open tasks、claimable tasks
  ← required reading
  ← ready-to-run next_calls
Agent → POST /v1/orientation/ack（读取宪法后确认）
Agent → workspace_sync / list_documents / query_workspace
```

`system/*` 由人类维护；Agent 只能提出提案。每个 namespace 的 readme 可声明 `required_frontmatter` 和 `doc_key_pattern`，写入时产生 warning，strict 模式下返回 422。

### 6.2 任务领取与 handoff

```text
POST /v1/tasks/{key}/claim
  → 行级锁 + TTL lease + lease_epoch + version + 1
  → audit + TASK_CLAIMED + outbox
  ← task + 上游 depends_on 的 L1 brief
    + input_context 引用 slots
    + 文档摘要（不含正文）

POST /v1/tasks/{key}/handoff
  → 校验 holder / 活动租约 / lease_epoch
  → 必填 L1 brief（summary、key_metrics、unverified_hypotheses、risk_flag）
  → 可选 L2 artifact → artifact/<task_key>/<id> 文档
  → 释放租约、状态流转、audit、timeline、outbox
  → DONE 触发下游依赖解锁
```

下游默认只拿 L1；需要全文时显式 `dereference(doc://...)`。

### 6.3 统一租约与 fencing

- 任务和状态槽都有 TTL 租约；默认 60 秒，可由任务 schema 约束在 5–3600 秒。
- claim 原子递增 `lease_epoch`；renew/handoff 必须匹配当前 holder、活动租约和可选 epoch。
- 数据库 `NOW()` 判断过期；reaper 回收时再次递增 epoch。
- 旧 Agent 即使恢复，也不能用过期 epoch 继续写入。
- `leases` 表统一记录资源、holder、heartbeat、progress、fencing token、结束原因；`GET /v1/leases` 可按 holder/state 查看。

### 6.4 状态槽 JSON Patch

```text
POST /v1/slots/{slot_key}/patch
  ├── 权限 state:write
  ├── locked_by_human → 403 HUMAN_SOVEREIGNTY_LOCKED
  ├── expected_version 不匹配 → 409 + latest
  ├── 其他 Agent 活动租约 / lease_epoch 不匹配 → 409 lease_conflict
  ├── jsonpatch 应用到 current_state
  ├── version + 1、audit UPDATE、SLOT_PATCHED
  └── 触发器 → NOTIFY → 控制台实时刷新
```

### 6.5 人类审批、人类主权与时间旅行

- 控制台通过 `/v1/human/tasks/{key}/review` 或 `/v1/admin/workspaces/{id}/tasks/{key}/review` 审批/驳回。
- 状态槽/文档通过 human/admin PUT 设置 `locked_by_human=true` 和 `human_note`。
- 人类覆盖会清除 Agent 租约、version + 1、写 audit，并广播 `HUMAN_OVERRIDE`。
- Agent 后续写入返回 403，直到人类解锁。
- 每次 Agent 写操作写入 `state_audit_diffs`；人类可按 `session_batch_id` 撤销整批。
- Undo 先从新到旧 dry-run，比较当前状态与 after-image；有漂移返回 `409 undo_conflict`，可显式 `force=true`。
- Undo 本身作为新的 audit batch 记录；timeline/outbox 不删除，外部副作用不自动逆转。

### 6.6 SSE 实时链路

```text
INSERT/UPDATE 表
  → PostgreSQL trigger notify_workspace_change()
  → 事务内 INSERT workspace_events（outbox，单调 BIGSERIAL id）
  → pg_notify('workspace_events', {id, table, workspace_id, ...})
  → app/events.py 的单 LISTEN 连接扇出到订阅队列
  → /v1/stream 或 /v1/admin/workspaces/{id}/stream
  → 连接时按 Last-Event-ID 从 outbox 重放缺失事件，再转实时
  → 按 workspace/table/status/assigned_role/namespace 过滤
  → 前端 ReadableStream 解析 id/event/data
  → 防抖刷新当前视图
```

### 6.7 提案、三方 rebase 与污点

```text
Agent POST /v1/documents/{doc}/proposals (base_version, content, mode)
  → 策略校验 + 污点预览 + 提案落库
  → 其他 Agent POST /v1/proposals/{id}/vote（0–5）
  → Human POST /v1/proposals/{id}/apply
      ├─ base == current：直接应用
      └─ base 过期：base/current/proposed 三方合并
            ├─ 不重叠：合并并记录 applied_version / rebase=true
            └─ 重叠：conflict_details + 409 proposal_conflict
```

污点传播：

- 从 `frontmatter.sources / derived_from / source_documents` 和正文 `doc://` 引用抽取来源。
- 外部 URL、显式 taint、未验证 UNTRUSTED 文档作为来源时，目标文档标记 `tainted=true` 并写 `taint_edges`。
- 递归重算下游文档、槽位、任务；默认查询排除 tainted 文档。
- Agent 验证 tainted 文档返回 `409 taint_blocked`；人类验证会清根并重算下游。

### 6.8 异步索引与预算

- 文档写入后进入 `document_index` 队列；index worker 重建 chunks、向量和检索索引。
- `GET /v1/index/status` 返回 pending/failed/oldest pending/p95 lag。
- 生产使用 Jina `jina-embeddings-v3`（1024 维，OpenAI-compatible endpoint）。
- 文档写入只 embed chunks，文档级向量取 chunk 均值；查询 fallback 的 `_embed` 文本截断到 6,000 字符，避免 Jina 8194 token 限制。
- `workspace_budgets` + `usage_ledger` + `rate_limit_windows` 控制月 token/成本、每分钟请求和 embedding 用量；超限返回 `402 budget_exceeded` 或 `429 rate_limited`。
- `BUDGET_MODE=shadow` 时只记录 would-limit，不拦截；`enforce` 时真正拦截。

### 6.9 导出 / 导入与租户生命周期

- `POST /v1/exports` 或 admin 导出任务：导出租户/工作区/namespace 的文档、任务、槽位、版本和元数据。
- `GET /v1/exports/{id}` 查询状态与计数；`GET /v1/exports/{id}/download` 下载 zip。
- `POST /v1/imports` 支持作为新租户、导入现有租户或增量导入。
- 租户暂停后读取/导出继续，写入被拒绝；删除需要 operator + owner 双方确认，30 天宽限期，最终导出保留。

---

## 7. 对外路径与接口

### 7.1 服务入口

| 路径 | 说明 |
|---|---|
| `GET /` | 服务描述。`Accept: text/html` 时返回 v6 人类视图（JSON 响应式布局 + 动态背景 + `Open console` 入口）；API 客户端继续拿到原 JSON（name/version/rest/mcp/health/agent_docs/first_call/primitives）。响应带 `Vary: Accept` 与 `Cache-Control: no-store` |
| `GET /?format=json` | 强制返回原始服务描述 JSON，供人类页面的 `RAW` 链接使用 |
| `GET /healthz` | 存活探针，返回状态和版本 |
| `GET /readyz` | 就绪探针：数据库、embedding provider/model/dimensions、MCP、版本 |
| `GET /docs` | Swagger UI；`DOCS_REQUIRE_AUTH=true` 时受 HTTP Basic 保护，密码为 `ADMIN_TOKEN` |
| `GET /openapi.json` | OpenAPI；生产默认受 Basic 保护 |
| `GET /dashboard/` | Next.js 静态控制台 |
| `GET /dashboard/login/` | 客户登录/注册页 |
| `GET /dashboard/connect/` | 部署 operator 入口（`ADMIN_TOKEN`） |
| `/v1/*` | Agent + commander REST |
| `/v1/auth/*` | 人类认证 |
| `/v1/human/*` | 登录后的人类控制台 API |
| `/v1/admin/*` | Operator API |
| `/v1/stream` | Agent/commander SSE |
| `/v1/admin/workspaces/{id}/stream` | 人类控制台 SSE |
| `/mcp/` | MCP streamable HTTP（Bearer token） |
| `python -m app.mcp_server` | MCP stdio |

### 7.2 REST 端点分组

| 分组 | 代表路径 |
|---|---|
| **Orientation / 身份** | `GET /v1/orientation`, `POST /v1/orientation/ack`, `GET /v1/whoami`, `GET /v1/agent-docs`, `GET /v1/openapi.json` |
| **任务** | `GET/POST /v1/tasks`, `GET /v1/tasks/{key}`, `POST /v1/tasks/{key}/claim|renew|release|handoff|review|escalate` |
| **统一租约** | `POST /v1/leases/{lease_id}/heartbeat|release`, `GET /v1/leases` |
| **状态槽** | `GET /v1/slots`, `GET/PUT /v1/slots/{key}`, `POST /v1/slots/{key}/patch` |
| **文档** | `GET/PUT /v1/documents/{key}`, `GET /v1/documents`, `POST /v1/documents/{key}/patch-section`, `.../verify`, `.../submit-review`, `.../deprecate`, `.../archive`, `.../restore`, `.../rename`, `.../parts`, `.../purge` |
| **版本 / 解释** | `GET /v1/documents/{key}/versions[/{version}]`, `GET /v1/documents/{key}/explain` |
| **批量 / 解析** | `POST /v1/documents/batch`, `POST /v1/dereference-many`, `POST /v1/dereference` |
| **查询** | `POST /v1/query`（frontmatter + 语义/关键词/hybrid） |
| **提案** | `POST/GET /v1/documents/{key}/proposals`, `GET /v1/proposals`, `GET /v1/proposals/{id}`, `POST /v1/proposals/{id}/vote|apply|reject` |
| **策略 / 污点** | `GET /v1/policies`, `POST /v1/documents/{key}/taint` |
| **命名空间 / 地址** | `GET/POST /v1/namespaces`, `POST /v1/documents/{key}/rename` |
| **时间线 / 链接** | `POST/GET /v1/timeline`, `POST/GET /v1/links` |
| **审计 / 统计** | `GET /v1/audit`, `GET /v1/stats` |
| **令牌 / 谱系** | `GET /v1/tokens/{ref}`, `.../lineage`, `.../tree` |
| **预算 / 索引** | `GET /v1/usage`, `GET /v1/usage/embedding`, `GET /v1/index/status` |
| **导出 / 导入** | `POST/GET /v1/exports`, `GET /v1/exports/{id}`, `GET /v1/exports/{id}/download`, `POST /v1/imports` |
| **外部动作** | `POST/GET /v1/external-actions` |
| **人类认证** | `GET /v1/auth/config`, `POST /v1/auth/register|login|google|google/link|password|logout`, `GET /v1/auth/me` |
| **人类控制台** | `/v1/human/*`：workspace、tasks review/escalate、slots、documents、audit、proposals、policies、webhooks、external-actions、budget、accounts、tenant、transfer-responsibilities |
| **Operator** | `/v1/admin/*`：workspaces、tasks、slots、documents、agent-tokens、audit undo、policies、budget、tenants、exports/imports、reindex、normalize-html、force-release/reassign/cancel |

### 7.3 MCP 工具（88 个）

| 能力组 | 覆盖内容 |
|---|---|
| **入职 / 身份** | orientation、constitution 确认、Agent 名录、子令牌/轮换/撤销、token tree 与 lineage |
| **任务 / 协作** | 任务创建、claim/heartbeat/handoff/review、escalation、continuation、子任务、任务树 |
| **状态 / 租约** | 状态槽 JSON Patch、heartbeat、release_lease、list_leases |
| **文档 / 检索** | upsert、批量写、section patch、query、列举、dereference、归档/恢复、提案、日志、recall、taint |
| **治理 / 租户** | 预算、告警、metrics、租户树、策略、共享、成员/权限相关的只读与治理入口 |
| **事件 / 导出 / 外部动作** | poll_events、subscribe/unsubscribe、export/import、external action、API docs |

当前共 **88 个 MCP 工具**，完整名称和参数以 `app/mcp_server/server.py`、`GET /v1/agent-docs` 和 `GET /v1/openapi.json` 为准。

MCP HTTP 默认无状态（`MCP_STATELESS_HTTP=true`），不返回 `mcp-session-id`；未知工具返回结构化 `unknown_tool` 404，而不是 500。stdio 模式通过 `MCP_API_KEY` 读取 Agent 身份。

### 7.4 人类控制台

| Tab | 内容 |
|---|---|
| **Blackboard** | 人类审批队列、实时状态网格、人类覆盖、升级/中断流 |
| **Tasks** | Kanban、L1 brief、租约、ping-pong 计数、审批/驳回、详情 |
| **Documents** | 命名空间树、验证/信任徽章、frontmatter、Markdown 预览/编辑、版本历史、Verify/Deprecate、人类锁 |
| **Agents** | 角色说明、Agent Token 铸造/停用/轮换、namespace |
| **Governance** | 提案、策略、Webhook、外部动作、预算、人类账号 |
| **Audit** | 会话批次、before/after diff、冲突感知 Undo、Force retry |

界面支持 EN/中文切换，语言写入 `localStorage.agentgraph.lang`，同时更新 `<html lang>`、日期/数字格式和全部 UI 文案。

### 7.5 错误与协议约定

- 错误体统一为 `{code, http_status, message, details}`；部分错误附加 `class`, `action`, `retryable`, `retry_after_ms`。
- 写请求支持 `Idempotency-Key`（REST）或 `idempotency_key`（MCP），24 小时重放窗口。
- 客户端可用 `AgentGraph-Version` 声明语义版本；默认 `2026-10-21`，新地址语法为 `2026-11`。
- 常见错误：`version_conflict`、`lease_conflict`、`HUMAN_SOVEREIGNTY_LOCKED`、`escalated_to_human`、`undo_conflict`、`proposal_conflict`、`taint_blocked`、`budget_exceeded`、`rate_limited`、`overloaded`、`unknown_tool`。
- 错误码完整列表见 `docs/ERROR_CODES.md`。

---

## 8. 权限与安全

### 8.1 Principal 类型

| 类型 | 来源 | 说明 |
|---|---|---|
| `workspace` | `ag_...` workspace key | 自动化/控制主体，**不是人类会话** |
| `agent` | `agt_...` scoped token | 角色 + namespace + grant snapshot |
| `human` | `agh_...` 人类会话 | 登录后的 workspace 成员 |
| `human_admin` | `ADMIN_TOKEN` | 部署 operator，可跨工作区 |

### 8.2 Agent 角色

| 能力 | SCOUT | ANALYST | AUDITOR | EXECUTOR | COMMANDER |
|---|:---:|:---:|:---:|:---:|:---:|
| workspace / task 读取 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 状态槽读取 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 状态槽写入 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 文档读取 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 文档创建 | ✅ | ✅ | ❌ | ✅ | ✅ |
| 文档更新 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 文档验证 | ❌ | ❌ | ✅ | ❌ | ✅ |
| 任务创建 | ❌ | ✅ | ❌ | ✅ | ✅ |
| 任务 claim / handoff | ❌ | ✅ | ✅ | ✅ | ✅ |
| 任务 review | ❌ | ❌ | ✅ | ❌ | ✅ |
| 审计读取 | ❌ | ❌ | ✅ | ❌ | ✅ |
| 提案投票 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 外部动作提议 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 人类覆盖 / 解锁 | ❌ | ❌ | ❌ | ❌ | ✅ |

> 以 `app/auth.py` 的 `PERMISSIONS` 为准；Agent Token 的 `allowed_namespaces` 同时约束文档、状态槽、任务、timeline、提案、审计、链接、SSE 和外部动作。

### 8.3 人类角色

- `OWNER`：工作区所有者，可管理预算、策略、Webhook、reindex、账号、租户删除、transfer responsibilities。
- `REVIEWER`：可执行绝大多数控制台审批/编辑操作，但不能执行上述 owner-only 操作。
- `VIEWER`：只读；不能改人类锁、taint、提案或外部动作。

新自助注册账号创建 `OWNER`，默认工作区 tier 为 `free`。`VIEWER` 账号不会获得通过 `locked_by_human` 的能力。

### 8.4 隔离与安全边界

- **租户**：workspace 归属租户树；`tenant_id` 由数据库推导，客户端发送会返回 `400 tenant_id_not_allowed`。
- **RLS**：`agentgraph_app` 是无登录、无 `BYPASSRLS` 的数据库角色；每个事务设置 `app.workspace_id` / `app.tenant_scope`，所有 workspace 表 `FORCE ROW LEVEL SECURITY`。上下文激活失败 → 503 fail-closed。
- **namespace**：提案、审计、时间线、任务上下文、links、SSE、外部动作全部校验 `allowed_namespaces`。
- **人类门禁**：`AWAITING_HUMAN` / `ESCALATED_TO_HUMAN` 只能由真实人类或 operator 处理；workspace key 不能绕过。
- **出站**：DNS 只解析一次并固定 IP；拒绝私网/回环/CGNAT；Webhook 每次发送重新校验；响应体有大小上限和总超时；生产强制 HTTPS。
- **浏览器**：Operator token 放在 `sessionStorage`；CSP/HSTS/X-Frame-Options/nosniff/Referrer-Policy/Permissions-Policy 由应用输出；Markdown 外部图片不自动加载。
- **文档**：`system/*` 是平台/人类维护；所有 Agent 可读平台 system 文档，但不能直接改写。
- **Secrets**：`ADMIN_TOKEN`、`POSTGRES_PASSWORD`、Jina/OpenAI key、Google client secret 只在服务器 `.env`，权限 600；不进入版本库。

---

## 9. 部署与运行

### 9.1 本地开发

```bash
cd E:\Codes\AgentGraph
cp .env.example .env
# 至少设置强 ADMIN_TOKEN；生产建议配置 Jina/OpenAI embedding
docker compose up -d --build
```

打开：

- 控制台：`http://localhost:8000/dashboard/`
- API docs：`http://localhost:8000/docs`
- MCP HTTP：`http://localhost:8000/mcp/`
- 健康检查：`http://localhost:8000/healthz` / `/readyz`

创建 workspace：

```bash
docker compose exec api python -m app.cli create-workspace --name "My Workspace"
```

### 9.2 Docker 镜像

`Dockerfile` 是两阶段构建：

```text
node:20-alpine
  └── npm ci + next build → /dashboard/out

python:3.12-slim
  ├── pip install -r requirements.txt
  ├── COPY app /app/app
  ├── COPY sql /app/sql
  ├── COPY scripts /app/scripts
  ├── COPY --from=dashboard /dashboard/out /app/dashboard/out
  └── uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
```

### 9.3 生产部署（Tlines）

| 项 | 值 |
|---|---|
| 服务器目录 | `/home/deploy/agentgraph` |
| Compose | `docker-compose.prod.yml`（项目名 `agentgraph`） |
| API 容器 | `agentgraph-api` |
| 镜像 | `agentgraph:3.2.1-veynod`（内含当前 v3.26 schema 代码） |
| 数据库 | 独立 `agentgraph_pgdata` 卷，不与其他项目共用 |
| 网络 | 内部 `agentgraph_internal` + 共享 `web`；不发布宿主端口 |
| 反向代理 | Caddy：`veynod.com` → `agentgraph-api:8000` |
| 备用域名 | `agentgraph.fusionpropulsion.space`、`agentgraph.104-207-82-85.nip.io` |
| 主控制台 | `https://veynod.com/dashboard/` |
| MCP | `https://veynod.com/mcp/` |
| 健康检查 | `https://veynod.com/readyz` |
| Admin Token | `/home/deploy/agentgraph/.env` 的 `ADMIN_TOKEN`，权限 600 |
| 回滚镜像 | `agentgraph:pre-v3.15-rollback` |

生产 compose 的关键环境：

```text
ENVIRONMENT=production
AUTO_INIT_DB=true
RLS_ENABLED=true
RLS_ROLE=agentgraph_app
MCP_ENABLED=true
OUTBOUND_ALLOW_PRIVATE_NETWORKS=false
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=jina-embeddings-v3
OPENAI_BASE_URL=https://api.jina.ai/v1
```

### 9.4 Schema 迁移链

```text
全新库
  sql/init.sql
  → v3_hardening.sql
  → v3_2_production.sql
  → v3_2_1_mcp_improvements.sql
  → v3_3 ... v3_29

v2 库
  v2_to_v3_collab.sql
  → 同上

已有 v3 库
  schema_migrations 中缺失的版本按顺序执行
```

迁移由 `app/database.py` 在 PostgreSQL advisory lock 下执行；多副本不会并发跑 DDL。当前 schema 版本记录在 `schema_migrations`，最新为 **3.29.0**。

### 9.5 备份与回滚

```bash
# 迁移前备份
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U agentgraph -d agentgraph --no-owner --clean --if-exists \
  > backups/agentgraph-$(date +%Y%m%d-%H%M%S).dump

# 回滚镜像（示例；具体版本按部署记录）
docker compose -f docker-compose.prod.yml up -d --no-build api
```

生产数据库备份放在 `/home/deploy/agentgraph/backups/`，权限 600。回滚前先确认 schema 迁移是否可逆；当前迁移以向前兼容为主，必要时使用数据库副本恢复。

---

## 10. 测试与验证

当前 `pytest --collect-only` 收集 **204 项测试**。完整集成测试需要 `TEST_DATABASE_URL` 指向 PostgreSQL + pgvector。

```bash
# 一次性测试库
docker run -d --name agentgraph-test-db \
  -e POSTGRES_USER=agentgraph -e POSTGRES_PASSWORD=agentgraph \
  -e POSTGRES_DB=agentgraph -p 55432:5432 \
  -v "$PWD/sql/init.sql:/docker-entrypoint-initdb.d/001_init.sql:ro" \
  pgvector/pgvector:pg16

export TEST_DATABASE_URL="postgresql+asyncpg://agentgraph:agentgraph@localhost:55432/agentgraph"
pytest -q
ruff check .
```

覆盖范围：

- 黑板书引擎：JSON Patch、section patch、租约、L1 校验；
- 任务：claim/renew/handoff/review、并发 claim、依赖环、ping-pong、reaper；
- 文档：版本、验证、信任、人类锁、提案、三方 rebase、软归档；
- 安全：namespace、人类门禁、SSRF、RLS、审计主体、CSP；
- 租户：树约束、暂停/恢复、双方确认删除、导出/导入；
- 存储：穷举列表、游标、批量读写、增量同步、规范 ID/别名/墓碑；
- 协议：错误注册表、API 版本、幂等、失败模式；
- 检索：chunking、hybrid RRF、embedding 维度/零填充；
- MCP：工具对等、结构化错误、unknown_tool 404；
- 前端：dashboard 静态托管、登录/连接路径。

生产验证记录包括：`/readyz`、REST/MCP smoke、任务 `claim → heartbeat → handoff → review`、迁移副本测试、检索基准。完整 204 项套件在本机需要先启动测试数据库；本轮文档更新未修改业务代码。

---

## 11. 当前边界与后续方向

**已经落地：**

- 多租户 + RLS + 租户树；
- 规范 ID、别名重定向、墓碑、namespace 注册；
- 统一租约 + fencing + 孤儿回收；
- L0/L1/L2、任务状态机、ping-pong 熔断；
- 文档版本、验证、信任、提案 Arena、三方 rebase；
- 污点传播；
- 预算、限流、embedding 配额、循环熔断；
- 异步索引 + HNSW/tsvector/trigram 混合检索；
- Webhook、外部动作、补偿；
- SSE outbox 重放；
- 租户导出/导入；
- 88 个 MCP 工具；
- EN/中文控制台、登录/注册、operator 入口分离；
- 生产 HTTPS、Caddy、健康检查。

**仍未包含：**

- OIDC / WebAuthn / MFA；
- 邮箱验证投递、忘记密码邮件；
- 语言感知的中文分词（当前 `simple` + trigram）；
- 跨区域部署与异地恢复演练；
- Playwright/axe 自动化视觉回归；
- 亮色主题（token 层已准备，产品仍为深色）；
- 根页面已上线 JSON-first 人类视图：浏览器 `Accept: text/html` 返回 HTML（动态网络背景、响应式 JSON 展示、Copy/RAW、dashboard 高亮入口），API 客户端和 `?format=json` 继续返回原 JSON；`Vary: Accept` 防止缓存串用。

相关文档：

- `docs/AGENT_GUIDE.md` — Agent 连接与调用指南；
- `docs/ERROR_CODES.md` — 错误码；
- `docs/AGENT_FEEDBACK_2026-10-21.md` — Agent 反馈与存储格式 backlog；
- `docs/SECURITY_AUDIT_2026-10-20.md` — 安全审计；
- `docs/v3-requirements.md` — 需求与规格；
- `docs/benchmarks/latest.md` — 检索基准；
- `docs/design-system/` — 设计 token、验收和本地设计稿。

---

## 12. GitHub R1/R2 增量（schema 3.26.0）

2026-10-22 合并 `origin/main` 后，代码已包含 GitHub 上的 R1.3–R2.4 能力；产品版本仍统一为 **v3.2.1**，数据库 schema 升至 **3.26.0**。

- **事件与订阅**：`v3_17_events.sql`，`app/services/events.py`，MCP `poll_events` / `subscribe` / `list_subscriptions` / `unsubscribe`。
- **人类与 Agent 引导**：`v3_18`/`v3_19`，`agent_onboarding.py`、`human_onboarding.py`、`docs_site.py`，控制台 checklist 与公开 docs 站点。
- **持久化与事件响应**：`v3_20`，`backups.py`、`incidents.py`、`alerts.py`、`mailer.py`、`access_log.py`。
- **token tree 与协作者**：`v3_21`，`token_tree.py`、`directory.py`，子令牌/轮换/撤销/谱系。
- **成员、角色与个人权利**：`v3_22`，`members.py`、`rights.py`、`org_routes.py`。
- **协作原语**：`v3_23`，`collab.py`，任务指针、mention、continuation、子任务与任务树。
- **溯源、召回与令牌预算**：`v3_24`，`provenance.py`、`recall.py`、`token_budget.py`。
- **日志、争用与态势**：`v3_25`，`logs.py`、`contention.py`、`situation.py`。
- **租户树**：`v3_26`，`tree.py`，子租户创建/移动/挂载/卸载。
- **Phase 3 R3.2 与缺口**：`v3_28`/`v3_29`，`retrieval_eval.py`（`eval/retrieval/` 评测集与基线）、`attestation.py` 与 `/.well-known/agentgraph/…`（`scripts/verify_lineage.py` 验签）、`export_schedule.py`、`workspace_events` 按 `shard_key` 16 个哈希分区、索引队列按分片轮询、控制台待审核汇总与熔断器面板。
- **Phase 3 R3.1**：`v3_27`，`lifecycle.py`、`digest.py`、`reviews.py`、`resources.py`，`/v1/webhooks`（`webhooks:manage` 范围）、预留 `room_id`、宪法修订规则展示。
- **前端新增**：`dashboard/app/docs/` 公开文档站，`components/onboarding.tsx`、`components/situation.tsx`，以及成员/权限、备份与事故相关控制台入口。
- **MCP 工具**：从 40 增至 **88**；完整名称见 `app/mcp_server/server.py`。

本次本地新增的根页面 JSON-first 视图和命名空间模糊选择器也在此版本内；生产、GitHub、本地应保持同一提交与 `3.2.1` 应用版本。

## 13. 快速索引

| 想找什么 | 去哪里 |
|---|---|
| FastAPI 入口 | `app/main.py` |
| 环境变量 | `app/config.py`, `.env.example` |
| DB schema / 迁移 | `app/database.py`, `sql/init.sql`, `sql/migrations/` |
| 认证与权限 | `app/auth.py`, `app/tenant.py` |
| 任务/租约 | `app/services/tasks.py`, `app/services/leases.py` |
| 状态槽 | `app/services/slots.py`, `app/engine/blackboard.py` |
| 文档/版本/检索 | `app/services/documents.py`, `app/services/retrieval.py`, `app/engine/chunking.py` |
| 提案/污点 | `app/services/proposals.py`, `app/services/taint.py` |
| 审计/undo | `app/services/audit.py`, `app/engine/merge.py` |
| 事件/SSE | `app/events.py`, `app/api/stream_routes.py` |
| 人类账号 | `app/services/accounts.py`, `app/api/auth_routes.py` |
| 人类控制台 API | `app/api/human_routes.py` |
| Operator API | `app/api/admin_routes.py` |
| MCP 40 工具 | `app/mcp_server/server.py` |
| 控制台前端 | `dashboard/app/` |
| 本地设计系统 | `docs/design-system/` |
| 部署 | `Dockerfile`, `docker-compose.prod.yml` |
| 冒烟测试 | `scripts/smoke_test.py` |

---

*AgentGraph v3.2.1 · schema 3.26.0 — 共享黑板、租约、人类主权、可追溯文档、提案与污点、租户 RLS、混合检索、签名 Webhook、可补偿外部动作、预算与可重放事件。*
