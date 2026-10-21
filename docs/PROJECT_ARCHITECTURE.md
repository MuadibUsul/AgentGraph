# AgentGraph v3.2.1 项目架构与路径说明

> 版本：v3.2.1  
> 定位：**人机共驾的多 Agent 共享工作空间 / 黑板系统（Human-in-the-Loop Shared Blackboard）**  
> 技术栈：FastAPI + PostgreSQL 16 + pgvector + MCP + Next.js 14  
> 当前状态：v3.2.1 已完成并部署到 Tlines 服务器；100 项自动化测试通过，生产 HTTPS 控制台、cookbook/benchmark 与设计验收已执行

---

## 1. 项目介绍

AgentGraph 最初是一个“Agent 原生记忆数据库”。v3.0 将它重构为一个**多 Agent 与人类共同读写的工作空间**；v3.1 补齐了可重放事件 outbox、fencing token、不可变文档版本与哈希绑定验证；v3.2 继续补齐生产边界：**受审提案与 Proposal Arena、路径策略/JSON Schema、污点传播、人类账号与 PostgreSQL RLS、已审批外部动作与补偿、持久化签名 Webhook、分块混合检索、预算与限流**。Agent 通过 REST / MCP 领取任务、读写共享状态与文档；人类通过 Web 控制台审批、冻结关键状态、查看审计并撤销 Agent 的整个会话。

它要解决的不是“记忆怎么存”，而是多 Agent 真正协作时的工程问题：

1. **上下文爆炸**：上游 Agent 不能把几万字日志直接塞给下游。
2. **并发覆盖与死锁**：任务和状态需要原子认领、TTL 租约与自动回收。
3. **Agent 无限扯皮**：生成者/审核者循环驳回会烧穿预算，需要硬熔断。
4. **人类指令被覆盖**：人类修正过的状态和文档必须拥有最高主权。
5. **权限失控**：公网采集 Agent 不能持有核心执行权限。
6. **盲目轮询**：Agent 需要事件驱动唤醒，而不是每秒 `SELECT`。

### 1.1 六个核心机制

| 机制 | 实现 |
|---|---|
| **渐进式披露 L0 / L1 / L2** | L0 工作区地图 → L1 结构化 handoff brief → L2 文档 artifact，通过 `doc://` URI 按需 `dereference` |
| **TTL 租约 + 局部修补** | 任务/槽位认领租约、心跳续期、后台回收；状态用 RFC 6902 JSON Patch，文档按标题局部替换 |
| **乒乓死循环熔断** | `ping_pong_count` 达到 `max_retries` 自动强制 `ESCALATED_TO_HUMAN`，阻断 Agent 继续重试 |
| **响应式可重放事件** | 触发器写事务性 `workspace_events` outbox → `NOTIFY` id → 每进程单 LISTEN → SSE；`Last-Event-ID` 断线重放 |
| **人类最高主权 + 冲突感知回滚** | `locked_by_human` 物理拦截 Agent；`state_audit_diffs` 按批次回滚，回滚前比对 after-image，冲突 409，可显式 force |
| **文档版本与可信状态** | `document_versions` 不可变版本 + SHA-256；验证绑定版本哈希；内容变更自动失效为 DRAFT |
| **可信内容标记** | `trust_level`；SCOUT 文档 UNTRUSTED，Agent 视图包裹 `[[UNTRUSTED_SOURCE]]` |
| **租约 fencing** | 单调 `lease_epoch` + 数据库 `NOW()`；过期 Agent 不能继续 handoff/写入 |
| **Agent 权限沙盒** | `SCOUT / ANALYST / AUDITOR / EXECUTOR` 角色令牌 + 命名空间限制 |

### 1.2 与 v2.0 的区别

- **保留**：workspaces 多租户、API Key、FastAPI、PostgreSQL、MCP、Next.js 控制台、timeline。
- **升级**：`context_slots` → 实时状态网格；`timeline_streams` → 实时事件总线。
- **改造**：`memory_crystals` → `shared_documents`；`causal_edges` → `workspace_links`。
- **新增（v3.0）**：`collab_tasks`、`state_audit_diffs`、`agent_tokens`、人类锁、批次回滚、SSE。
- **新增（v3.1）**：`document_versions`、`workspace_events` outbox、验证/信任字段、`lease_epoch`、冲突检测回滚、`Last-Event-ID` 重放。
- **新增（v3.2）**：`document_proposals` / `proposal_votes`、`path_policies`、`human_users` / `human_sessions`、`taint_edges`、`document_chunks`、`webhook_rules` / `webhook_deliveries`、`external_actions` / `external_action_attempts`、`workspace_budgets` / `usage_ledger` / `rate_limit_windows`。
- **废弃**：艾宾浩斯衰减、salience 排序、`crystallize / context_pull / assert_state / traverse_cause` 旧原语。
- **兼容**：现有 v2 数据库原地迁移，workspace commander key 继续有效。

> v2 的数据迁移结果已验证：7 条 memory crystals → 7 篇 shared documents，3 条 causal edges → 3 条 workspace links，context slots 与 timeline 全部保留。

---

## 2. 总体架构

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                          Human Commander (Web UI)                           │
│  Next.js 14 静态控制台：Blackboard · Tasks · Documents · Agents · Audit      │
│  人工审批 · 状态覆写 · 文档编辑 · 批次撤销 · SSE 实时刷新                    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ HTTPS / REST + SSE
┌───────────────────────────────────▼──────────────────────────────────────────┐
│                         AgentGraph Gateway (FastAPI)                         │
│                                                                              │
│  Auth & Principal          Task Board Engine        Context/State Engine      │
│  ─ commander key           ─ claim/renew/release    ─ JSON Patch (RFC 6902)  │
│  ─ agent tokens            ─ TTL lease              ─ TTL slot lease         │
│  ─ RBAC + namespaces       ─ handoff / review       ─ human sovereignty      │
│                            ─ ping-pong breaker                                │
│                                                                              │
│  Document Engine           Audit / Undo             Realtime Event Bus        │
│  ─ frontmatter + Markdown  ─ before/after images    ─ LISTEN/NOTIFY           │
│  ─ heading section patch   ─ session batch undo     ─ SSE filters             │
│  ─ vector query            ─ timeline events                                  │
│                                                                              │
│  MCP Server: stdio + streamable HTTP (22 tools, same service layer)           │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ Async SQLAlchemy / asyncpg
┌───────────────────────────────────▼──────────────────────────────────────────┐
│                       PostgreSQL 16 + pgvector                               │
│                                                                              │
│  workspaces          agent_tokens        shared_documents                    │
│  context_slots       collab_tasks        workspace_links                     │
│  timeline_streams    state_audit_diffs   document_versions                   │
│  workspace_events    schema_migrations                                       │
│                                                                              │
│  DB triggers ──► workspace_events (outbox) ──► pg_notify id ──► SSE replay  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 架构选择

- **单引擎存储**：不引入 Redis / Kafka / 外部任务队列。实时事件、任务状态、文档和审计全部在 PostgreSQL 内完成。
- **同一套服务层**：REST、admin 控制台和 MCP 共用 `app/services/`，避免三套业务逻辑漂移。
- **乐观锁优先**：文档、槽位、任务都有 `version`；并发冲突返回 409 和最新状态，由调用方重新对齐。
- **审计即产品能力**：每次 Agent 写入都记录前后镜像；回滚不是日志重放，而是有版本控制的恢复。
- **人类主权不可绕过**：Agent 角色即使拥有 `state:write`，也无法覆盖 `locked_by_human = true` 的资源。

---

## 3. 完整目录与路径

```text
E:\Codes\AgentGraph\
├── .dockerignore
├── .env                                  # 本地运行配置（gitignore，不提交）
├── .env.example                          # 全部环境变量模板
├── .gitignore
├── Dockerfile                            # 多阶段：构建 Next.js + 安装 Python 运行时
├── docker-compose.yml                    # api + pgvector/pg16，含 env_file 转发
├── pyproject.toml                        # ruff / pytest 配置
├── requirements.txt                      # 运行依赖（含 jsonpatch）
├── requirements-dev.txt                  # pytest / pytest-asyncio / ruff
├── README.md                             # 快速开始、API/MCP、部署说明
│
├── app/
│   ├── __init__.py
│   ├── main.py                           # FastAPI 入口、lifespan、异常处理、MCP/静态看板挂载
│   ├── config.py                         # Pydantic Settings：版本、租约、配额、SSE、MCP
│   ├── auth.py                           # commander/agent Principal、API Key、RBAC 矩阵
│   ├── database.py                       # async engine/session、advisory lock、v2→v3.1→v3.2 迁移/回填
│   ├── models.py                         # 全部 v3.2 表的 SQLAlchemy ORM
│   ├── events.py                         # 每进程单 LISTEN 连接 → SSE 订阅队列
│   ├── schemas.py                        # 全部请求/响应 Pydantic 模型与 handoff 契约
│   ├── errors.py                         # 统一错误码：version_conflict / HUMAN_SOVEREIGNTY_LOCKED 等
│   ├── middleware.py                     # 纯 ASGI 请求日志、X-Request-ID
│   ├── cli.py                            # init-db / workspace / agent-token / stats CLI
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── blackboard.py                 # 租约、JSON Patch、Markdown 段落修补、L1 brief 校验
│   │   ├── embeddings.py                 # OpenAI / Ollama / 离线 hash 向量化
│   │   └── formatter.py                  # YAML frontmatter、handoff/task/slot 的 LLM 视图
│   │
│   ├── services/                         # 领域服务（REST + MCP 共用）
│   │   ├── __init__.py                   # 统一导出
│   │   ├── common.py                     # 序列化、audit、timeline、snapshot
│   │   ├── audit.py                      # 批次查询 + 冲突感知回滚 + force
│   │   ├── documents.py                  # 文档版本/验证/信任、section patch、artifact、语义查询
│   │   ├── links.py                      # workspace_links 创建/查询
│   │   ├── reaper.py                     # 后台租约回收循环
│   │   ├── slots.py                      # 状态网格、JSON Patch、人类锁
│   │   ├── tasks.py                      # claim/lease/handoff/review/escalate/依赖解锁
│   │   ├── timeline.py                   # timeline 追加/读取
│   │   └── workspace.py                  # 工作区、L0 map、stats、agent token
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── v1_routes.py                  # Agent/commander REST API
│   │   ├── admin_routes.py               # 人类控制台 API（admin token）
│   │   └── stream_routes.py              # 可重放 SSE：Last-Event-ID + outbox replay
│   │
│   └── mcp_server/
│       ├── __init__.py
│       ├── __main__.py                   # python -m app.mcp_server（stdio）
│       └── server.py                     # 22 个 MCP 工具 + HTTP/stdio 认证
│
├── sql/
│   ├── init.sql                          # 基础 DDL + outbox 触发器（幂等）
│   └── migrations/
│       ├── v2_to_v3_collab.sql           # v2→v3 原地迁移（保留数据）
│       ├── v3_hardening.sql              # 版本/信任/fencing/outbox 增量迁移
│       ├── v3_2_production.sql           # v3.2 生产扩展（提案/策略/污点/账号/RLS/检索/出站）
│       ├── v3_2_1_mcp_improvements.sql   # MCP 评审/反馈
│       ├── v3_3_agent_feedback.sql       # 软归档
│       ├── v3_4_security_hardening.sql   # 安全加固（提案 namespace/Google sub/审计主体/RLS）
│       ├── v3_5_artifact_binding.sql     # 任务产出物版本/哈希绑定与交接人记录
│       ├── v3_6_failure_protocol.sql     # 失败语义协议：幂等记录（AG-ST-18）
│       ├── v3_7_tenants.sql              # 租户树、tenant_id 全表推导、成员、RLS 租户范围（AG-ID-07）
│       └── v3_8_addresses.sql            # canonical ID、别名重定向、墓碑、namespace 注册、$ref 引用（AG-ST-17）
│
├── dashboard/
│   ├── package.json                      # Next.js 14.2 / React 18 / react-markdown
│   ├── package-lock.json
│   ├── next.config.mjs                   # output: export, basePath: /dashboard
│   ├── tsconfig.json
│   ├── next-env.d.ts
│   └── app/
│       ├── layout.tsx                    # 根布局、全局 CSS 引入
│       ├── page.tsx                      # 控制台外壳、连接、数据加载、SSE、弹窗
│       ├── types.ts                      # 前端类型与时间/状态工具
│       ├── globals.css                   # 基础设计系统与 v2 共用样式
│       ├── v3.css                        # v3 看板/状态网格/文档/审计样式
│       └── components/
│           ├── ui.tsx                    # Icon、Modal、状态/角色徽章、Empty、Spinner
│           ├── blackboard.tsx            # 审批队列 + 实时状态网格 + 中断流
│           ├── task-board.tsx            # Kanban + 任务详情/审批弹窗 + 新建任务
│           ├── documents.tsx             # 文档树、验证/信任、版本历史、Markdown 编辑
│           ├── agents.tsx                # 角色说明、Agent Token 铸造/停用
│           └── audit.tsx                 # 会话批次、diff 查看、Undo
│
├── tests/
│   ├── conftest.py                       # 启动真实 uvicorn + 测试数据库夹具
│   ├── test_blackboard.py                # JSON Patch / section patch / 租约 / L1 校验
│   ├── test_formatter.py                 # frontmatter 输出、L1/task/slot 渲染
│   ├── test_schemas.py                   # 输入校验、RBAC 约束、handoff 契约
│   ├── test_embeddings.py                # hash 向量、维度、归一化、零填充
│   ├── test_v3_integration.py            # 端到端：任务/槽位/文档/SSE/MCP/租约回收
│   └── test_hardening.py                 # 回滚冲突、版本/验证、SSE 重放、并发 claim、fencing、依赖环
│
├── scripts/
│   └── smoke_test.py                     # 部署后端到端冒烟脚本
│
├── docs/
│   ├── v3-requirements.md                # 合并后的 v3 正式需求/规格
│   └── PROJECT_ARCHITECTURE.md           # 本文档
│
└── backups/                              # 本地迁移备份（gitignore，不提交）
    ├── v2_before_v3.sql
    ├── v2_before_v3_live.sql
    └── v3_before_hardening.sql
```

> 说明：`dashboard/out` 是 `npm run build` 生成的静态产物，Docker 镜像会内置；本地运行时也会被 FastAPI 自动托管。`node_modules`、`.venv`、`.next`、缓存和 `backups` 不进入版本库。

---

## 4. 模块职责

### 4.1 入口与基础设施

| 文件 | 职责 |
|---|---|
| `app/main.py` | 创建 FastAPI；启动时执行 schema/migration、启动租约回收器、启动 MCP session manager；挂载 REST、admin、SSE、`/mcp`、`/dashboard`；统一异常格式；健康检查。 |
| `app/config.py` | 从环境变量读入所有设置；校验向量维度、权重/配额等；暴露 `tier_limits()`。 |
| `app/database.py` | 创建 async engine/session；启动时检测 `memory_crystals` 并执行 v2→v3 迁移；执行幂等 `init.sql`。 |
| `app/auth.py` | API Key 哈希、Agent Key 生成；把 bearer token 解析为 `Principal`（COMMANDER 或 4 种 Agent 角色）；权限矩阵、命名空间、人类锁与 Agent 身份绑定。 |
| `app/errors.py` | 统一 `{"error": {code, message, details}}`；包括 `HUMAN_SOVEREIGNTY_LOCKED`、`version_conflict`、`lease_conflict`、`escalated_to_human`。 |
| `app/middleware.py` | 纯 ASGI 请求日志和 `X-Request-ID`，不会阻断 SSE 流。 |
| `app/cli.py` | `init-db`、`create-workspace`、`create-agent-token`、`list-workspaces`、`rotate-key`、`stats`。 |

### 4.2 引擎层

| 文件 | 职责 |
|---|---|
| `engine/blackboard.py` | 租约计算；RFC 6902 JSON Patch；Markdown 标题定位与段落替换；L1 handoff brief 强制字段校验；`doc://` URI 解析。 |
| `engine/embeddings.py` | 异步向量化抽象；OpenAI、Ollama、离线 hash；1536 维校验；Ollama 小维度零填充（余弦不变）。 |
| `engine/formatter.py` | 把 ORM 对象转为 Agent 友好的 YAML frontmatter + Markdown；L1 brief、任务卡、状态槽、timeline 的紧凑文本。 |

### 4.3 服务层

| 文件 | 关键逻辑 |
|---|---|
| `services/tasks.py` | 任务创建/依赖环检测；原子 claim + `lease_epoch`；DB 时钟续期/释放；handoff（L1+L2）；review 与乒乓熔断；escalate；租约回收；依赖满足自动 READY。 |
| `services/slots.py` | 状态槽替换/JSON Patch；Agent 租约 + `lease_epoch`；人类覆盖与解锁；租约回收。 |
| `services/documents.py` | 文档创建/更新（版本锁）；不可变 `document_versions`；验证/信任状态；按标题 patch；frontmatter 过滤 + 向量查询；artifact 生成；人类编辑锁。 |
| `services/audit.py` | before/after 镜像；按 `session_batch_id` 聚合；dry-run 冲突检测；倒序恢复；INSERT 批次删除；回滚本身再次审计。 |
| `services/workspace.py` | 工作区 CRUD；L0 map；统计与配额；Agent Token 铸造/停用。 |
| `services/links.py` | 任务/文档/槽位之间的 typed links；self-loop 与存在性校验。 |
| `services/timeline.py` | timeline 追加、权限与命名空间检查、最近事件读取。 |
| `services/reaper.py` | 后台循环：过期任务 lease → READY；过期槽位 lease → 清锁；清理过期 outbox。 |
| `services/common.py` | 序列化、snapshot、audit 写入、DB 时钟、事件写入。 |
| `app/events.py` | 单进程单 LISTEN 连接，向所有 SSE 订阅队列扇出 workspace 事件。 |

### 4.4 API 层

| 文件 | 说明 |
|---|---|
| `api/v1_routes.py` | Agent/commander REST：whoami、L0 map、tasks、slots、documents（含 verify/deprecate/versions）、query、dereference、links、timeline、audit、stats。 |
| `api/admin_routes.py` | 人类控制台：workspace、任务审批/升级、槽位人类覆盖、文档创建/编辑/验证/版本、Agent Token、审计与带 force 的批次撤销。 |
| `api/stream_routes.py` | 可重放 SSE：`Last-Event-ID`/`last_event_id` 从 outbox 补齐断线事件；单进程 bus，不占请求池。 |
| `mcp_server/server.py` | 22 个 MCP 工具；HTTP 中间件解析 workspace/agent token；stdio 从 `MCP_API_KEY` 读取身份；公共服务层复用 RBAC。 |

### 4.5 前端

| 文件 | 视图 |
|---|---|
| `page.tsx` | 连接控制面、工作区切换、统计卡、Tab、SSE 重连、数据刷新、创建/密钥/任务/槽位弹窗。 |
| `components/blackboard.tsx` | 人类审批队列、实时状态网格、HUMAN_OVERRIDE/ESCALATION 中断流。 |
| `components/task-board.tsx` | 4 列 Kanban、L1 摘要、租约/PP 计数器、审批/驳回、任务详情。 |
| `components/documents.tsx` | 命名空间树、验证/信任徽章、frontmatter、Markdown 预览/编辑、版本历史、Verify/Deprecate、人类锁。 |
| `components/agents.tsx` | 角色权限说明、Token 铸造、命名空间、启停。 |
| `components/audit.tsx` | 批次卡、before/after diff、冲突提示与 Force Undo。 |

---

## 5. 数据模型

### 5.1 表关系

```text
workspaces (tenant + commander key hash)
   │ 1
   ├──< agent_tokens          (scoped agent keys, role, namespaces)
   ├──< shared_documents      (doc_key unique per workspace)
   ├──< context_slots         (slot_key unique per workspace)
   ├──< collab_tasks          (task_key unique per workspace)
   ├──< workspace_links       (source_id/target_id can point to task/doc/slot)
   ├──< timeline_streams      (append-only event audit)
   ├──< state_audit_diffs     (before/after images by session_batch_id)
   ├──< document_versions     (immutable content versions per document)
   └──< workspace_events      (transactional outbox with monotonic id)

collab_tasks ──artifact_uri──► shared_documents (doc://<doc_key>)
collab_tasks ──input_context──► context_slots / shared_documents (按 key 引用)
workspace_links ──depends_on──► 任务依赖；DONE 后自动解锁下游 TODO → READY
```

### 5.2 关键字段

| 表 | 关键字段 |
|---|---|
| `workspaces` | `id`, `name`, `api_key_hash`, `tier`, `created_at` |
| `agent_tokens` | `agent_signature`, `role_scope`, `allowed_namespaces`, `api_key_hash`, `is_active` |
| `shared_documents` | `doc_key`, `title`, `content`, `content_hash`, `frontmatter`, `verification_status`, `trust_level`, `verified_version`, `verified_hash`, `embedding`, `embedding_model`, `namespace`, `version`, `locked_by_human` |
| `context_slots` | `slot_key`, `current_state`, `schema_hint`, `blockers`, `namespace`, `version`, `lease_epoch`, `locked_by`, `lock_expires_at`, `locked_by_human` |
| `collab_tasks` | `status`, `priority`, `assigned_role`, `claimed_by`, `lease_expires_at`, `lease_epoch`, `handoff_brief`, `artifact_uri`, `ping_pong_count`, `max_retries`, `version` |
| `state_audit_diffs` | `session_batch_id`, `agent_signature`, `target_table`, `target_record_id`, `before_diff`, `after_diff` |
| `timeline_streams` | `event_type`, `payload`, `namespace`, `token_cost`, `latency_ms` |
| `document_versions` | `document_id`, `version`, `parent_version`, `content_hash`, `title/content/frontmatter`, `author`, `session_batch_id` |
| `workspace_events` | monotonic `id`, `source_table`, `action`, record keys, status/role/namespace, `payload`, `created_at` |

### 5.3 任务状态机

```text
create ──► READY / TODO
TODO ──依赖全部 DONE──► READY
READY / HANDOFF ──claim──► IN_PROGRESS
IN_PROGRESS ──handoff──► REVIEWING | HANDOFF | DONE | AWAITING_HUMAN | FAILED
REVIEWING ──approve──► DONE
REVIEWING ──reject──► IN_PROGRESS  (ping_pong_count += 1)
ping_pong_count >= max_retries ──► ESCALATED_TO_HUMAN
lease 过期 ──reaper──► READY（同时 lease_epoch + 1，旧持有者写入失效）
人类审批/驳回 ──► DONE / HANDOFF / READY / IN_PROGRESS（重置 PP 计数）
```

所有 claim/renew/handoff 使用数据库 `NOW()` 判断租约；非 commander 的 handoff/renew
还必须通过当前持有者、活动租约和可选 `lease_epoch` 校验。

---

## 6. 关键流程

### 6.1 认证与租户隔离

1. 客户端发送 `Authorization: Bearer ag_xxx`（commander）或 `agt_xxx`（agent）。
2. `app/auth.py` 对 Key 做 SHA-256，查 `workspaces` 或 `agent_tokens`。
3. 生成 `Principal`：workspace、role、agent_signature、allowed_namespaces。
4. 每个服务查询都显式带 `workspace_id`；Agent 额外校验角色和 namespace。
5. 人类后台用 `ADMIN_TOKEN`，通过 `principal_for_admin()` 以 COMMANDER 身份操作。

### 6.2 Agent 领取任务并读取 L1

```text
Agent → POST /v1/tasks/{key}/claim
      → 行级锁 + 租约 + version + 1 + audit + TASK_CLAIMED 事件
      → 返回 task + 上游 depends_on 任务的 L1 brief
        + input_context 引用的 slots（完整状态）
        + 文档摘要（不含正文）
```

下游 Agent 默认只拿到 L1；需要核查全文时调用 `dereference(artifact_uri)`。

### 6.3 Handoff 与 L2 artifact

1. Agent 提交 `next_status` + 必填 L1 brief + 可选 `artifact_text`。
2. 服务生成 `artifact/<task_key>/<id>` 文档，`artifact_uri = doc://…`。
3. 释放租约、更新状态、写 audit、发 `TASK_HANDOFF` timeline 事件。
4. `REVIEWING` 任务进入审核；`DONE` 触发下游依赖解锁。
5. 可附带 `create_next` 自动创建下游任务。

### 6.4 状态槽 JSON Patch

```text
POST /v1/slots/{slot_key}/patch
  ├── 权限 state:write
  ├── locked_by_human → 403 HUMAN_SOVEREIGNTY_LOCKED
  ├── expected_version 不匹配 → 409 + latest
  ├── 其他 Agent 活动租约 / lease_epoch 不匹配 → 409 lease_conflict
  ├── jsonpatch 应用到 current_state
  ├── version + 1、audit UPDATE、SLOT_PATCHED
  └── 数据库触发器 → NOTIFY → 控制台实时刷新
```

### 6.5 人类审批 / 人类主权

- 控制台通过 `/v1/admin/workspaces/{id}/tasks/{key}/review` 审批或驳回，重置 ping-pong 计数。
- 状态槽/文档通过 admin PUT 设置 `locked_by_human=true` 和 `human_note`。
- 该操作会清除 Agent 租约、version + 1、写 audit，并广播 `HUMAN_OVERRIDE` 中断。
- Agent 后续写入返回 403，直到人类解锁。

### 6.6 批次审计与时间旅行

```text
每次 Agent 写操作
  → state_audit_diffs(before_diff, after_diff, session_batch_id)
  → 人类在 Audit 选择批次
  → 加行锁；对每条 diff 从新到旧 dry-run，比较当前快照与 after_diff
  → 有漂移 → 409 undo_conflict（列出字段）；人类可显式 force
  → 无冲突/force → 倒序应用 before_diff；INSERT 批次删除目标行
  → version + 1（不倒退版本）；文档重新嵌入并追加新版本
  → 回滚本身作为新的 undo batch 再次审计
  → 发 BATCH_UNDONE 事件，响应提示事件/外部副作用不可撤销
```

### 6.7 SSE 实时链路

```text
INSERT/UPDATE 表
  → PostgreSQL trigger notify_workspace_change()
  → 事务内 INSERT workspace_events（outbox，单调 BIGSERIAL id）
  → pg_notify('workspace_events', {id, table, workspace_id, ...})
  → app/events.py 的每进程单 LISTEN 连接扇出到订阅队列
  → /v1/stream 或 admin /stream
  → 连接时按 Last-Event-ID 从 outbox 重放缺失事件，再转实时
  → 按 workspace/table/status/assigned_role/namespace 过滤
  → 前端 fetch ReadableStream 解析 id/event/data
  → 450ms 防抖刷新看板
```

### 6.9 文档版本、验证与可信标记

```text
文档创建/更新/section patch/人类编辑
  → content_hash = SHA-256(content)
  → INSERT document_versions(version, parent_version, hash, author, batch)
  → 内容变更时：verification_status = DRAFT, verified_* = NULL
  → AUDITOR/COMMANDER 调 /verify：
      绑定 verified_version = 当前 version, verified_hash = 当前 hash
      verification_status = VERIFIED, trust_level = TRUSTED
  → /v1/query 默认只查 VERIFIED；include_unverified=true 才查草稿
  → SCOUT 来源文档 trust_level=UNTRUSTED，agent_view 包裹
    [[UNTRUSTED_SOURCE]] … [[/UNTRUSTED_SOURCE]]
```

### 6.8 MCP

- **HTTP**：`POST/GET http://<host>:8000/mcp/`，Bearer 为 commander 或 agent token。
- **stdio**：`python -m app.mcp_server`，需要 `DATABASE_URL` + `MCP_API_KEY`。
- 22 个工具全部复用 `app/services/`，所以 RBAC、人类锁、审计与 REST 完全一致。

---

## 7. REST API 与 MCP 工具

### 7.1 Agent / commander API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/whoami` | 当前 workspace/role/namespace |
| GET | `/v1/workspace/map` | L0 地图 |
| GET/POST | `/v1/tasks` | 任务列表 / 创建 |
| GET | `/v1/tasks/{task_key}` | 任务详情 |
| POST | `/v1/tasks/{task_key}/claim` | 领取 + 租约 + L1 上下文 |
| POST | `/v1/tasks/{task_key}/renew` | 租约心跳 |
| POST | `/v1/tasks/{task_key}/release` | 释放回 READY |
| POST | `/v1/tasks/{task_key}/handoff` | L1/L2 交接 |
| POST | `/v1/tasks/{task_key}/review` | 审核/驳回（熔断） |
| POST | `/v1/tasks/{task_key}/escalate` | 升级给人类 |
| GET | `/v1/slots` | 状态网格 |
| GET | `/v1/slots/{slot_key}` | 单槽状态 |
| PUT | `/v1/slots/{slot_key}` | 全量替换 / 创建 |
| POST | `/v1/slots/{slot_key}/patch` | JSON Patch |
| GET/PUT | `/v1/documents[/{doc_key}]` | 文档列表 / 读取 / upsert |
| POST | `/v1/documents/{doc_key}/patch-section` | 按标题局部修改 |
| POST | `/v1/documents/{doc_key}/verify` | 哈希绑定验证当前版本 |
| POST | `/v1/documents/{doc_key}/submit-review` | 提交待审 |
| POST | `/v1/documents/{doc_key}/deprecate` | 弃用 |
| GET | `/v1/documents/{doc_key}/versions[/{version}]` | 不可变版本历史 |
| POST | `/v1/query` | frontmatter + 向量查询（默认仅 VERIFIED） |
| GET | `/v1/agent-docs` | Agent token 可读的 OpenAPI/连接指南 |
| GET | `/v1/documents?namespace=&cursor=&limit=&include_archived=` | 游标全量列举 |
| POST | `/v1/documents/batch` | 最多 25 个文档，单事务 |
| POST | `/v1/dereference-many` | 最多 25 个 URI 批量解析 |
| POST | `/v1/documents/{doc_key}/archive` / `restore` | 软归档/恢复 |
| POST | `/v1/dereference` | L2 按需解析 |
| POST/GET | `/v1/links` | 创建/查询 typed links |
| POST/GET | `/v1/timeline` | 追加/读取事件 |
| GET | `/v1/audit` | AUDITOR/COMMANDER 读取审计 |
| GET | `/v1/stats` | 统计与配额 |
| GET | `/v1/stream` | Agent SSE（Last-Event-ID 可重放） |

### 7.2 人类控制台 API（Admin Token）

`/v1/admin/workspaces`、`.../stats`、`.../timeline`、`.../tasks`、`.../tasks/{key}/review|escalate`、`.../slots`、`.../slots/{slot_key}`、`.../documents`（创建/读取/人类编辑/verify/deprecate/versions）、`.../agent-tokens`、`.../audit`、`.../batches/{batch_id}/undo?force=true`、`.../stream`。

### 7.3 MCP 工具

| 工具 | 作用 |
|---|---|
| `workspace_sync` | L0 快照；`since=<cursor>` 增量同步，含 `namespace_counts` 与 `next_cursor` |
| `agent_claim_and_read` | 领取任务 + L1 上下文 |
| `agent_patch_shared_state` | 状态 JSON Patch |
| `agent_handoff_task` | L1/L2 交接、创建下游 |
| `agent_escalate_to_human` | 升级 |
| `upsert_shared_doc` | 文档创建/更新 |
| `patch_doc_section` | Markdown 段落修改 |
| `query_workspace` | frontmatter + 语义查询；默认仅 VERIFIED，可传 `include_unverified=true` |
| `list_documents` | 命名空间全量游标列举：`total`、namespace/状态/污点/更新时间筛选与排序，默认排除归档 |
| `dereference` | 按需加载 L2 |
| `dereference_many` | 最多 25 个 URI 批量解析，逐项返回 ok/error |
| `upsert_many` | 最多 25 个文档、单事务全成功或全回滚 |
| `archive_document` / `restore_document` | 软归档/恢复（作者或 COMMANDER），写审计与时间线 |
| `get_api_docs` | Agent 可读的连接指南与请求 schema |

Handoff 工具可携带 claim 返回的 `lease_epoch`；服务端校验持有者、活动租约与 epoch。

---

## 8. 权限矩阵

| 能力 | SCOUT | ANALYST | AUDITOR | EXECUTOR | COMMANDER |
|---|:---:|:---:|:---:|:---:|:---:|
| workspace / task 读取 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 状态槽读取 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 状态槽写入 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 文档读取 | 仅 namespace | ✅ | ✅ | ✅ | ✅ |
| 文档创建 | 仅 raw namespace | ✅ | ❌ | ✅ | ✅ |
| 文档更新 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 文档验证 / 弃用 | ❌ | ❌ | ✅ | ❌ | ✅ |
| 文档版本历史 | 仅 namespace | ✅ | ✅ | ✅ | ✅ |
| 任务创建 | ❌ | ✅ | ❌ | ✅ | ✅ |
| 任务 claim / handoff | ❌ | ✅ | ✅ | ✅ | ✅ |
| 任务 review | ❌ | ❌ | ✅ | ❌ | ✅ |
| 审计读取 | ❌ | ❌ | ✅ | ❌ | ✅ |
| 人类覆盖 / 解锁 | ❌ | ❌ | ❌ | ❌ | ✅ |

- Agent Token 的 `allowed_namespaces` 同时约束文档、状态槽、任务和 timeline。
- `SCOUT` 的典型配置是 `allowed_namespaces=["raw_inbox"]`。

---

## 9. 部署与运行路径

### 9.1 开发环境

```text
本机 8000 端口  FastAPI + 内置静态看板
本机 5432/55432 PostgreSQL（Docker）
.env             DATABASE_URL / ADMIN_TOKEN / EMBEDDING_PROVIDER
```

### 9.2 Docker Compose

```text
docker-compose.yml
  ├── db  : pgvector/pgvector:pg16
  │         └── 首次启动挂载 sql/init.sql
  └── api : Dockerfile 多阶段构建
            ├── 阶段 dashboard：npm ci + next build
            ├── 阶段 runtime：python:3.12-slim + requirements.txt
            └── 启动时 AUTO_INIT_DB=true → advisory lock → v2/v3.0→v3.1 迁移与回填 → uvicorn
```

对外端口：`http://localhost:8000`  
静态看板：`http://localhost:8000/dashboard/`  
MCP 流式 HTTP：`http://localhost:8000/mcp/`

### 9.3 迁移路径

```text
v2 数据库（memory_crystals / causal_edges）
  → app/database.py 检测 to_regclass('memory_crystals')
  → sql/migrations/v2_to_v3_collab.sql
      rename memory_crystals → shared_documents
      rename causal_edges → workspace_links
      add doc_key/title/frontmatter/version/locked_by_human
      drop salience/decay/access 旧列
      add agent_tokens / collab_tasks / state_audit_diffs
      install NOTIFY trigger
  → sql/init.sql 幂等补齐索引与表
  → 若 schema 仍是 3.0：sql/migrations/v3_hardening.sql
      add content_hash / verification_status / trust_level / verified_*
      add embedding_model / lease_epoch
      create document_versions / workspace_events
      replace outbox-aware NOTIFY trigger
  → Python 回填每个文档的 SHA-256 与初始 document_versions
  → schema_migrations 记录 3.1.0
```

已有 v3.0 库也走同一 hardening 路径；全新库由 init.sql 直接创建 3.1 schema，
随后幂等 hardening 作为自检。两种情况都已验证：v2 dump 迁移后得到 7 个文档、
7 个初始版本、3 条链接、2 个槽位；fresh schema 的 100 项测试全部通过。

迁移前建议备份：

```bash
docker compose exec -T db pg_dump -U agentgraph -d agentgraph \
  --no-owner --clean --if-exists > backups/v2_before_v3.sql
```

---

## 10. 测试与验证

| 层 | 内容 |
|---|---|
| 单元测试 | JSON Patch、Markdown 段落、L1 brief、租约、formatter、schema 校验、hash 向量 |
| 集成测试 | 真实 uvicorn + pgvector：RBAC、任务租约/fencing、handoff/L2、熔断、人工锁、冲突感知回滚、文档版本/验证/信任、SSE 断线重放、10 并发 claim、依赖环、MCP、配额、reaper、看板 |
| 部署冒烟 | `scripts/smoke_test.py`：workspace → slot → task → claim → handoff → review → doc → query → audit |
| 迁移验证 | fresh 3.1 schema、v3.0→3.1 hardening、v2 dump→3.1 三条路径均验证；v2 数据 7 docs / 7 versions / 3 links / 2 slots 保留 |

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://agentgraph:agentgraph@localhost:55432/agentgraph"
pytest -q        # 当前 100 passed
ruff check .
```

---

## 11. 当前运行实例

截至本文档编写时，生产预览已部署到 Tlines 服务器：

- 控制台：`https://agentgraph.fusionpropulsion.space/dashboard/`
- API / MCP：`https://agentgraph.fusionpropulsion.space/v1`、`/mcp/`
- 备用域名：`https://agentgraph.104-207-82-85.nip.io/dashboard/`
- 版本：AgentGraph v3.2.1（`/readyz` 实测）
- 服务器目录：`/home/deploy/agentgraph`
- 编排：`docker-compose.prod.yml`（项目 `agentgraph`，API 容器 `agentgraph-api`，仅加入共享 `web` 网络，不占宿主端口）
- 反向代理：Caddy `agentgraph.fusionpropulsion.space` → `agentgraph-api:8000`（备用 nip.io 同配置）
- Admin Token：位于服务器 `/home/deploy/agentgraph/.env` 的 `ADMIN_TOKEN`（权限 600，不写入版本库）
- 工作区：`grokbot`（测试工作区）与 `AgentGraph Production`（空库新建，tier=pro）
- 没有预置 BTC/演示业务数据；所有统计来自实际 API/DB
- 数据库：独立 `agentgraph_pgdata` 卷；不复用 tline 数据库

> `agentgraph.fusionpropulsion.space` 使用 Namecheap DNS 的 A 记录指向
> `104.207.82.85`；Caddy 已签发证书并反代 `agentgraph-api:8000`。原
> `agentgraph.104-207-82-85.nip.io` 保留为备用入口。

---

## 12. v3.2 生产增量架构

### 12.1 新增服务与路径

| 路径 | 职责 |
|---|---|
| `app/engine/merge.py` | 行级三方合并；非重叠编辑合并，重叠/同位置不同插入返回冲突 |
| `app/engine/chunking.py` | 按 Markdown 标题与段落分块，供 HNSW/tsvector/trigram 检索 |
| `app/services/proposals.py` | 提案创建、投票、apply/reject、过期 base 自动 rebase、supersede |
| `app/services/policies.py` | glob 路径策略、角色/内容大小/JSON Schema 门禁 |
| `app/services/taint.py` | 来源抽取、污点边、下游文档/槽位/任务传播、人工清除 |
| `app/services/accounts.py` | scrypt 密码、人类会话、登录/登出/账号管理 |
| `app/services/retrieval.py` | 文档分块索引、向量+关键词 RRF 混合检索、全库重建 |
| `app/services/webhooks.py` | 持久化规则、outbox 匹配、HMAC 签名投递、指数退避 |
| `app/services/external_actions.py` | 审批式外发动作、幂等、尝试账本、补偿动作 |
| `app/services/usage.py` | 预算、用量账本、固定窗口限流、402/429 |
| `app/services/outbound.py` | 目标 URL 校验（HTTPS/allowlist/私网阻断）与 HTTP 投递 |
| `app/tenant.py` | `SET LOCAL ROLE agentgraph_app` + `app.workspace_id`，事务内 RLS 上下文 |
| `app/api/auth_routes.py` / `human_routes.py` | 人类会话 API 与 workspace 级人工操作 API |
| `dashboard/app/components/governance.tsx` | 提案/策略/Webhook/外部动作/预算/人类账号控制台 |
| `dashboard/app/i18n.tsx` | 中英双语上下文、持久化语言选择、语言切换按钮与格式化辅助 |
| `app/engine/notion.py` | Notion/HTML 检测与 Markdown 转换；表格、标题、列表、链接与安全过滤 |
| `dashboard/app/components/markdown.tsx` | GFM + sanitized raw HTML 渲染，兼容 Notion 表格 |
| `dashboard/app/design-tokens.css` | Primitive → Semantic → Component 设计 Token |
| `scripts/webhook_receiver.py` | Bot 侧签名 Webhook 接收器：校验时间戳/HMAC，写入 JSONL outbox |
| `app/services/tasks.py` | 失败认领/续租/释放/交接/审核/升级写入 `TASK_*_FAILED` 时间线 |
| `POST /v1/admin/workspaces/{id}/agent-tokens/{token_id}/rotate` | Agent 身份不变、密钥立即轮换 |
| `dashboard/app/v3.css` 末尾 v3.3 层 | 平坦表面、字号/间距/控件边界、标签页、看板、表格、移动端连接弹窗 |
| `app/api/auth_routes.py` | 邮箱注册、密码登录、Google ID token 登录、公开 auth config |
| `dashboard/app/page.tsx` 会话模式 | 注册/登录后使用 human session 访问 `/v1/human` 与租户 `/v1` 接口 |

### 12.2 提案与自动 rebase

```text
Agent POST /v1/documents/{doc_key}/proposals (base_version, content, mode)
  → 策略校验 + 污点预览 + 提案落库
  → 其他 Agent POST /v1/proposals/{id}/vote（0–5）
  → Human POST /v1/proposals/{id}/apply
      ├─ base == current：直接应用
      └─ base 过期：base/current/proposed 三方合并
            ├─ 不重叠：合并并记录 applied_version / rebase=true
            └─ 重叠：proposal.conflict_details + 409 proposal_conflict
```

文档写入与槽位 Patch 也接受 `rebase=true`：文档使用 `DocumentVersion` 作为 base；
槽位从 `state_audit_diffs` 找到 expected_version 的 after-image，只允许修改
base/current 中值相同的 JSON Pointer 路径。

### 12.3 污点传播

- 文档从 `frontmatter.sources / derived_from / source_documents` 与正文
  `doc://` 引用抽取来源。
- 来源是外部 URL、显式 taint，或未验证的 UNTRUSTED 文档时，目标文档标记
  `tainted=true` 并写 `taint_edges`。
- `propagate_from_document` 递归重算下游文档、槽位、任务；查询默认排除
  tainted 文档，Agent 视图包裹 `[[TAINTED_PROVENANCE]]`。
- Agent 验证 tainted 文档返回 `409 taint_blocked`；人类验证会清根并重算下游。
- 提案创建时先计算污点预览，避免应用后才发现不可信来源。

### 12.4 人类账号与 RLS

```text
POST /v1/auth/login → agh_... session (scrypt hash stored as SHA-256)
GET  /v1/auth/me
POST /v1/auth/logout
GET/POST/PATCH /v1/admin/workspaces/{id}/human-users
/v1/human/* → workspace-scoped review/undo/docs/slots/proposals/actions/webhooks/budget
```

`agentgraph_app` 是无登录、无 BYPASSRLS 的数据库角色。principal 解析后，
请求事务执行 `SET LOCAL ROLE agentgraph_app` 与
`set_config('app.workspace_id', …, true)`；SQLAlchemy `after_begin` 为后续事务
自动重新应用。所有 workspace 表启用 `FORCE ROW LEVEL SECURITY`，策略只允许
`workspace_id = current_setting('app.workspace_id')`；v3.4 移除了
`app.bypass_rls` 逃生开关，并把激活失败改为 503 fail-closed。
认证查询在上下文建立前以连接用户执行；后台分发器无 workspace 上下文，
以连接角色跨租户扫描，因此必须保持显式 workspace 过滤。

### 12.4.1 v3.4 安全边界

- **namespace**：提案、审计、时间线、任务上下文槽位、links、SSE 与外部动作
  全部执行 `allowed_namespaces`；受限 Agent 只能列举/读取自己的或已验证的文档。
- **人类门禁**：`AWAITING_HUMAN`/`ESCALATED_TO_HUMAN` 只能由真实人类账号处理；
  Agent 只能 handoff 到 `REVIEWING`/`HANDOFF`，不能写 `human_feedback`。
- **角色**：新成员默认 `VIEWER`；只有 `OWNER` 能改预算、策略、Webhook 和 reindex。
- **出站**：DNS 只解析一次并固定 IP；拒绝非 global/CGNAT 地址；每次 Webhook
  发送重新校验；响应体有字节上限和总超时；审批列表展示 payload/headers；
  补偿动作必须单独审批。
- **审计**：`state_audit_diffs` 记录 `actor_auth_kind`/`actor_token_id`/
  `actor_user_id`，即使 workspace key 指定了显示名也能识别真实认证主体。
- **浏览器**：Operator token 使用 sessionStorage；CSP/HSTS/X-Frame/nosniff/
  Referrer-Policy 由应用中间件输出；Markdown 外部图片不自动加载。

### 12.5 检索与索引

`document_chunks` 保存标题、内容、SHA-256、embedding 和 generated tsvector；
HNSW（m=16, ef_construction=64）、GIN(tsvector)、GIN(pg_trgm) 三类索引。
查询可显式选择 `semantic / keyword / hybrid / auto`；hybrid 用 reciprocal
rank fusion 合并两路排名，默认只查 VERIFIED 且非 tainted 文档。写入、撤销、
提案应用都会重建该文档 chunks；管理员可 `POST /workspaces/{id}/reindex`。

### 12.6 出站边界与补偿

- Webhook：持久化规则 + `last_event_id` 游标，事件匹配 event type/table/namespace；
  `X-AgentGraph-Signature` = HMAC-SHA256(timestamp + "." + body)，指数退避重试。
- External action：Agent 只能创建 PENDING_APPROVAL，人类批准后由后台分发；
  幂等键唯一、尝试账本、最大尝试次数；带 `compensation_payload` 的动作在 undo
  或人工补偿时生成 `compensation:<type>` 动作，并回写原动作 `COMPENSATED`。
- 出站 URL 在生产强制 HTTPS，支持 host allowlist，并默认阻断私网/回环地址。

### 12.7 租约/主权/外部动作安全边界

- workspace key 视为自动化主体，不是人类会话；只有 HUMAN/HUMAN_ADMIN 能绕过
  `locked_by_human`、应用提案、验证污染文档和批准外部动作。
- task renew/release/handoff 只允许当前 lease owner；管理员通过
  `POST /v1/admin/workspaces/{id}/tasks/{key}/force-release` 显式恢复卡死任务。
- external action 创建一律 `PENDING_APPROVAL`，不会因创建者身份自动执行。
- `GET /v1/auth/me` 仅接受 human session，workspace/agent key 返回
  `401 human_session_required`。
- 查询结果包含 RRF `score`，默认 `SEMANTIC_THRESHOLD=0.15` 过滤低相关结果；
  hash embedding 仍是词法近似，真实语义质量需 OpenAI/Ollama。
- Docker CMD 增加 `--proxy-headers --forwarded-allow-ips=*`；Caddy HTTP 308
  跳 HTTPS，生产 `/docs` 已在浏览器验证。
- 可丢弃的测试工作区可用
  `DELETE /v1/admin/workspaces/{id}?confirm=<精确名称>` 级联删除；生产清理优先
  归档/导出，不把硬删除当日常操作。

### 12.8 预算与限流

`workspace_budgets` 定义月 token/成本上限、每分钟请求上限和并发租约上限；
`usage_ledger` 记录 timeline token 与外发动作；`rate_limit_windows` 做数据库
固定窗口计数。超限分别返回 `402 budget_exceeded` 与 `429 rate_limited`，
Agent token 可覆盖 per-minute 与月 token 预算。

### 12.9 新增/扩展 API（摘要）

| 类别 | 端点 |
|---|---|
| 人类认证 | `POST /v1/auth/login`, `GET /v1/auth/me`, `POST /v1/auth/logout` |
| 人类控制台 | `/v1/human/*`（review、undo、docs、slots、proposals、webhooks、actions、budget、accounts） |
| 提案 | `POST/GET /v1/documents/{doc_key}/proposals`, `GET /v1/proposals`, `POST /v1/proposals/{id}/vote|apply|reject` |
| 策略 | `GET /v1/policies`; admin `/workspaces/{id}/policies` CRUD |
| 污点 | `POST /v1/documents/{doc_key}/taint`; admin 同路径 |
| 检索 | `POST /v1/query` 的 `mode/include_tainted`; admin `/reindex` |
| Webhook | admin `/workspaces/{id}/webhooks` CRUD + `/test` + `/deliveries` |
| 双语控制台 | 顶栏 EN / 中文切换，语言写入 `localStorage.agentgraph.lang`，同时更新 `<html lang>`、日期/数字格式与全部 UI 文案 |
| Notion 导入修复 | `/v1/admin/workspaces/{id}/documents/normalize-html` 批量/单篇修复；写入时自动把 Notion HTML 表格转换为 Markdown，前端再用 sanitized raw HTML 兜底 |
| 外部动作 | `POST/GET /v1/external-actions`; admin approve/reject/compensate |
| 预算/账号 | admin `/budget`, `/usage`, `/human-users` |

### 12.10 迁移与验证路径

1. 全新库：`init.sql`（v3.1 基础）→ `v3_hardening.sql` → `v3_2_production.sql`。
2. v3.1 库：`v3_2_production.sql`（全部 DDL 幂等、角色/策略可重入）→ 记录 `3.2.0`；v3.2.1 再执行 `v3_2_1_mcp_improvements.sql`。
3. v2 dump：`v2_to_v3_collab.sql` → hardening → v3.2；原有文档哈希/首版本回填。

验证：`100 passed`（fresh pgvector）、生产 HTTPS `/readyz` 返回 3.2.1、公开控制台
Governance 面板实际加载、`scripts/benchmark_retrieval.py` 生成真实 recall/latency 报告。

## 13. 扩展点与当前边界

v3.2 已落地：受审提案与 Arena、三方 rebase、路径策略/JSON Schema、污点传播、
人类账号与 PostgreSQL RLS、类型化链接外键、分块混合检索、持久化签名 Webhook、
审批式外部动作与补偿、预算/限流，以及覆盖并发/重放/epoch/提案/rebase/污点/RLS/
Webhook/外部动作、MCP 对等、失败操作时间线、agent token 轮换、review_feedback、游标列举、软归档、批量读写、范围过滤与增量同步的测试。

3.2.1 复测修复：驳回后任务回到 `READY` 并保留 `review_feedback`/`reviewed_by`，
租约清空；管理员可 `reassign`/`cancel` 历史 SCOUT 任务；MCP 返回值改为真正的
结构化对象（消除 `structuredContent.result` 二次编码），错误统一为
`{code, http_status, message, details}` 并同时写入文本与 `structuredContent`；
SDK 参数校验错误统一为 `validation_error`/422，不再返回 Pydantic 原文；
自投票错误包含提案与投票者 details；非审核态任务返回 `task_not_reviewable`
并说明实际 approve/reject 动作；MCP 的 `review_task.next_status` 统一为
`DONE/HANDOFF/READY`（退回默认 READY），`workspace_sync` 增加 `outputs`
产出列表，审核通过会自动把非污点 `doc://` 产物标记为 `VERIFIED/TRUSTED`；
`lease_seconds` schema 标明默认 60、范围 5–3600；MCP 默认返回 JSON 而非 SSE；
审核通过会自动验证非污点产物，`backfill-verified` 可补齐历史已批准产物并
多轮重算下游污点；所有审核退回写入 `reject_history` 并累加计数，重新提交
会把旧审核意见移入历史；人类/admin 会话固定使用自身身份，历史误归属通过
`REVIEW_ATTRIBUTION_CORRECTED` 事件修正；MCP 写入的 timeline 标记
`platform=mcp` 并使用文档命名空间；`session_batch_id` 在工具 schema 中为 UUID；
section patch 会自动去掉重复的标题行。

仍未包含：OIDC/WebAuthn/MFA；语言感知的中文分词；跨区域部署与异地恢复演练；
Playwright/axe 自动化视觉回归（当前设计验收标记为 NOT TESTED/BLOCKED）；亮色主题
（按既有深色控制台身份保留，Token 层已为后续亮色准备）。

这些内容已在 README 与 `docs/v3-requirements.md` 中保持诚实边界。下一轮存储层需求
（穷举列表、agent 可读文档、批量读写、归档、范围过滤、增量同步）见
`docs/AGENT_FEEDBACK_2026-10-21.md`。

---

*AgentGraph v3.2.1 — MCP/REST 对等、受审提案、污点传播、人类账号与 RLS、混合检索、签名 Webhook、
可补偿外部动作、预算、fencing 租约、可重放事件与冲突感知时间旅行。*
