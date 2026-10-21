"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { setFormatLanguage, type FormatLanguage } from "./types";

export type Language = FormatLanguage;

const ZH: Record<string, string> = {
  // Shell and navigation
  "Human · Agent Blackboard": "人类 · Agent 黑板",
  "Loading console…": "正在加载控制台…",
  "Checking gateway…": "正在检查网关…",
  "Gateway unreachable": "网关不可达",
  "Gateway online": "网关在线",
  Workspaces: "工作区",
  "Create workspace": "新建工作区",
  "Control plane": "控制平面",
  "Connection": "连接设置",
  "Connect to the AgentGraph API with the endpoint and admin token from your deployment.":
    "使用部署的 API 地址和管理员令牌连接到 AgentGraph。",
  "API endpoint": "API 地址",
  "Admin token": "管理员令牌",
  "Connect to gateway": "连接到网关",
  "Connecting…": "连接中…",
  Disconnect: "断开连接",
  "The token stays in this browser. Only use the console on a trusted machine.":
    "令牌保存在当前浏览器。请只在可信设备上使用控制台。",
  "Connect below to load workspaces.": "在下方连接以加载工作区。",
  "No workspaces yet. Create one to begin.": "还没有工作区。先创建一个开始。",
  Console: "控制台",
  Overview: "概览",
  LIVE: "实时",
  RECONNECTING: "重连中",
  "API docs": "API 文档",
  Refresh: "刷新",
  "New task": "新建任务",
  "Something went wrong.": "出了点问题。",
  "No workspace selected": "未选择工作区",
  "Create a workspace to provision a shared blackboard for your agent team.":
    "创建一个工作区，为你的 Agent 团队提供共享黑板。",
  "Connected to the AgentGraph control plane.": "已连接到 AgentGraph 控制平面。",
  "Skip to workspace content": "跳到工作区内容",
  "Dismiss notification": "关闭通知",
  "Awaiting human review: {count}": "待人工审核：{count}",
  Normal: "普通",
  "Unable to connect.": "无法连接。",
  "Unable to refresh workspaces.": "无法刷新工作区。",
  "Unable to load workspace.": "无法加载工作区。",
  "Unable to open document.": "无法打开文档。",
  "Enter the API endpoint and an admin token.": "请输入 API 地址和管理员令牌。",
  "API endpoint is required.": "请填写 API 地址。",
  "Admin token is required. Paste the ADMIN_TOKEN from the server.":
    "请填写管理员令牌。粘贴服务器上的 ADMIN_TOKEN。",
  "Admin token contains spaces. Paste it without spaces or line breaks.":
    "管理员令牌包含空格。请去掉空格或换行后再粘贴。",
  "Admin token is too short to be valid.": "管理员令牌太短，格式不正确。",
  "Admin token was rejected. It does not match this server's ADMIN_TOKEN.":
    "管理员令牌被拒绝，与本服务器的 ADMIN_TOKEN 不匹配。",
  "Admin API not found at this endpoint. Check the API endpoint URL.":
    "该地址没有管理 API，请检查 API 地址。",
  "Could not reach the API endpoint. Check the URL, CORS, and network.":
    "无法访问 API 地址。请检查地址、CORS 和网络。",
  "Commander key rotated.": "指挥官密钥已轮换。",
  "Unable to rotate key.": "无法轮换密钥。",
  "Task created and ready for claim.": "任务已创建，等待认领。",
  "Unable to create task.": "无法创建任务。",
  "Unable to load task.": "无法加载任务。",
  "Task escalated to human review.": "任务已升级给人工审核。",
  "Unable to review task.": "无法审核任务。",
  "State must be a JSON object": "状态必须是 JSON 对象",
  "Human lock released; agents may write again.": "人工锁已释放，Agent 可以重新写入。",
  "State override saved and human sovereignty locked.":
    "状态覆写已保存，并已加人工主权锁。",
  "Unable to update slot.": "无法更新状态槽。",
  "Clipboard access was blocked; select and copy the key manually.":
    "剪贴板访问被阻止；请手动选择并复制密钥。",
  "Unable to create workspace.": "无法创建工作区。",
  "Human-in-the-loop shared blackboard": "人机共驾的共享黑板",
  "Agents do the work.": "Agent 负责执行。",
  "Humans hold the line.": "人类守住底线。",
  "Shared documents, a live state grid, TTL task leases, progressive L1→L2 handoffs, a ping-pong circuit breaker, and session-batch undo — all in one PostgreSQL core with MCP-native access.":
    "共享文档、实时状态网格、TTL 任务租约、L1→L2 渐进披露、乒乓熔断和会话批次撤销——全部运行在一个 PostgreSQL 核心上，并原生支持 MCP。",
  "Connect the commander": "连接指挥官",
  "Enter the API endpoint and ADMIN_TOKEN from your deployment in the sidebar.":
    "在侧栏填写部署的 API 地址和 ADMIN_TOKEN。",
  "Mint scoped agents": "签发受限 Agent",
  "Give each runtime a role and namespace sandbox: SCOUT, ANALYST, AUDITOR, EXECUTOR.":
    "为每个运行时分配角色和命名空间沙箱：SCOUT、ANALYST、AUDITOR、EXECUTOR。",
  "Supervise live": "实时监督",
  "Approve escalated tasks, override locked state, inspect L2 artifacts, undo whole sessions.":
    "审批升级任务、覆写锁定状态、检查 L2 产物、撤销整个会话批次。",
  "Lease locks": "租约锁",
  "L1 / L2 handoff": "L1 / L2 交接",
  "Human sovereignty": "人类主权",
  "LISTEN / NOTIFY": "LISTEN / NOTIFY",
  "Batch undo": "批次撤销",
  "Shared workspace": "共享工作区",
  "Created": "创建于",
  "Active": "活跃于",
  "Rotate commander key": "轮换指挥官密钥",
  "Shared documents": "共享文档",
  "Tasks on the board": "任务看板",
  "State slots": "状态槽",
  "Scoped agents": "受限 Agent",
  "frontmatter + Markdown · versioned": "frontmatter + Markdown · 版本化",
  "claims, leases, review gates": "认领、租约、审核门禁",
  "human-locked live state": "人工锁定的实时状态",
  "{count} human decision(s) waiting": "{count} 个人工决策待处理",
  "Workspace views": "工作区视图",
  "New workspace": "新建工作区",
  "An isolated blackboard: documents, tasks, slots, agents and audit.":
    "一个隔离的黑板：文档、任务、状态槽、Agent 和审计。",
  Name: "名称",
  Tier: "套餐",
  "Developer · $5": "开发者 · $5",
  Free: "免费",
  Pro: "专业版",
  "Creating…": "创建中…",
  "Human override is version-checked and broadcast as an interrupt.":
    "人工覆写会校验版本，并作为中断事件广播。",
  "Current state (JSON)": "当前状态（JSON）",
  "Human note (broadcast to agents)": "人工备注（广播给 Agent）",
  "Risk off until CPI clears.": "CPI 公布前保持风险关闭。",
  "Unlock for agents": "对 Agent 解锁",
  "Save human override": "保存人工覆写",
  "Take human sovereignty lock (agents cannot write until unlocked)":
    "取得人类主权锁（解锁前 Agent 不能写入）",
  "Agent token minted. Store it in the runtime now.":
    "Agent 令牌已签发。立即保存到运行时。",
  "Commander key rotated. Update existing integrations.":
    "指挥官密钥已轮换。请更新现有集成。",
  "Workspace provisioned. Store the commander key now.":
    "工作区已创建。立即保存指挥官密钥。",
  Copied: "已复制",
  Copy: "复制",
  "I’ve stored it": "我已保存",
  "Use it as": "使用方式：",
  "Agent runtimes should instead receive a scoped": "Agent 运行时应改用受限的",
  "token from the Agents tab.": "令牌（Agents 页签发）。",
  "No frontmatter properties": "没有 frontmatter 属性",
  "Document unlocked; agents may write again.": "文档已解锁，Agent 可以重新写入。",
  "Unable to unlock document": "无法解锁文档",
  "Unable to save document": "无法保存文档",
  "Unable to create document": "无法创建文档",
  "Frontmatter must be a JSON object": "Frontmatter 必须是 JSON 对象",
  "Document verified against its current content hash.":
    "文档已按当前内容哈希完成验证。",
  "Unable to verify document": "无法验证文档",
  "Deprecate this document? Agents can still read it explicitly.":
    "废弃这篇文档？Agent 仍可显式读取。",
  "Document deprecated.": "文档已废弃。",
  "Unable to deprecate document": "无法废弃文档",
  "Unable to load history": "无法加载历史",
  "Unable to load version": "无法加载版本",
  Library: "文档库",
  "New document": "新建文档",
  "No documents": "没有文档",
  "Create one or let an agent hand off an artifact.":
    "创建一篇，或让 Agent 交接产物。",
  "Select a document": "选择文档",
  "Shared documents combine structured frontmatter with Markdown, like a machine-readable Notion page.":
    "共享文档把结构化 frontmatter 与 Markdown 结合，像机器可读的 Notion 页面。",
  TRUSTED: "可信",
  UNTRUSTED: "不可信",
  "Tainted provenance": "污染来源",
  "Derived from tainted or untrusted sources": "来源于被污染或不可信内容",
  "No versions recorded.": "没有版本记录。",
  "Select a version to inspect.": "选择一个版本查看。",
  "Immutable content versions; verification is bound to the current hash.":
    "不可变内容版本；验证绑定当前内容哈希。",
  "New shared document": "新建共享文档",
  "Document key": "文档键",
  Title: "标题",
  "Frontmatter (JSON properties)": "Frontmatter（JSON 属性）",
  "Markdown body": "Markdown 正文",
  "Human note (broadcast on save)": "人工备注（保存时广播）",
  "Frontmatter (JSON)": "Frontmatter（JSON）",
  "Create document": "创建文档",
  "Create a shared document": "创建共享文档",
  "Save + lock": "保存并锁定",
  "Unlock document": "解锁文档",
  "Verify current version": "验证当前版本",
  "Deprecate": "废弃",
  "History": "历史",
  "Edit": "编辑",
  "Preview": "预览",
  "Version": "版本",
  "Verification": "验证状态",
  "Updated": "更新于",
  "Role": "角色",
  "Status": "状态",
  "Allowed agent roles": "允许的 Agent 角色",
  "Agent signature": "Agent 标识",
  "Role scope": "角色范围",
  "Allowed namespaces (comma separated, * for all)":
    "允许的命名空间（逗号分隔，* 表示全部）",
  "Mint token": "签发令牌",
  "Mint an agent token": "签发 Agent 令牌",
  "The raw key is shown exactly once. Store it in the agent runtime.":
    "原始密钥只显示一次。请保存到 Agent 运行时。",
  "No agent tokens": "没有 Agent 令牌",
  "Mint a token for each agent runtime before connecting it to the API or MCP.":
    "在接入 API 或 MCP 前，为每个 Agent 运行时签发令牌。",
  "Scoped agent tokens": "受限 Agent 令牌",
  "Each token pins one agent signature, a role and its namespaces.":
    "每个令牌固定一个 Agent 标识、一个角色及其命名空间。",
  Disabled: "禁用",
  Disable: "禁用",
  Enable: "启用",
  Agent: "Agent",
  Namespaces: "命名空间",
  "Unable to create agent token": "无法创建 Agent 令牌",
  "Unable to update token": "无法更新令牌",
  "Session batches": "会话批次",
  "Every agent mutation records a before/after image. Undo restores the batch.":
    "每次 Agent 变更都会记录前后镜像。撤销会恢复整个批次。",
  "No audit batches": "没有审计批次",
  "Agent writes will appear here grouped by session_batch_id.":
    "Agent 写入会按 session_batch_id 分组显示在这里。",
  "Recent diffs": "最近差异",
  "Newest first. Each entry carries the full before-image used for rollback.":
    "最新在前。每条记录包含回滚使用的完整前镜像。",
  "No diffs": "没有差异",
  "Undo batch": "撤销批次",
  "Force undo": "强制撤销",
  "Force undo? This overwrites changes made after this batch.":
    "强制撤销？这会覆盖该批次之后发生的修改。",
  "Undo every mutation in this session batch? The restore is audited as a new batch.":
    "撤销该会话批次中的所有变更？恢复操作会作为新批次写入审计。",
  "Unable to undo batch": "无法撤销批次",
  "Human approval gate": "人工审批门",
  "Agents are paused until a human resolves these tasks.":
    "在人类处理这些任务前，Agent 处于暂停状态。",
  "No approvals waiting": "没有待审批任务",
  "Escalated or AWAITING_HUMAN tasks will appear here in real time.":
    "升级或处于 AWAITING_HUMAN 的任务会实时出现在这里。",
  "No L1 brief supplied": "未提供 L1 简报",
  "Live state grid": "实时状态网格",
  "Human edits take sovereignty and block agent writes until unlocked.":
    "人工编辑会取得主权，并在解锁前阻止 Agent 写入。",
  "No state slots": "没有状态槽",
  "Agents create slots with agent_patch_shared_state or the REST slot endpoint.":
    "Agent 通过 agent_patch_shared_state 或 REST 状态槽接口创建。",
  "Slot key": "状态槽键",
  "Current state": "当前状态",
  Governance: "治理",
  Open: "开放",
  Override: "覆写",
  "Interrupts & governance": "中断与治理",
  "Human overrides, escalations and batch rollbacks from the event bus.":
    "来自事件总线的人工覆写、升级和批次回滚。",
  "Quiet board": "安静看板",
  "HUMAN_OVERRIDE and escalation signals will stream here.":
    "HUMAN_OVERRIDE 与升级信号会实时显示在这里。",
  Tainted: "被污染",
  "{count} waiting": "{count} 个待处理",
  "{count} slots": "{count} 个状态槽",
  "{count} recent": "最近 {count} 条",
  "Task board": "任务看板",
  "Claims are lease-based. Rejections increment the ping-pong counter and escalate automatically at the threshold.":
    "认领基于租约。驳回会增加乒乓计数，达到阈值后自动升级。",
  "Backlog / Ready": "待办 / 就绪",
  "In progress": "进行中",
  "Human gate": "人工门禁",
  Closed: "已关闭",
  "No tasks": "没有任务",
  "Review ping-pong count": "审核乒乓计数",
  "lease live": "租约有效",
  "lease expired": "租约过期",
  unclaimed: "未认领",
  "Task input references tainted content": "任务输入引用了被污染内容",
  "Assigned role": "分配角色",
  "Any role": "任意角色",
  Priority: "优先级",
  Namespace: "命名空间",
  "Claim / lease": "认领 / 租约",
  "Ping-pong": "乒乓计数",
  "L1 handoff brief": "L1 交接简报",
  "No L1 brief yet.": "还没有 L1 简报。",
  "Input context": "输入上下文",
  "Output artifact": "输出产物",
  "Reject history": "驳回历史",
  "Human feedback": "人工反馈",
  "Approval note or rejection reason…": "审批备注或驳回原因…",
  "Approve": "通过",
  "Reject": "驳回",
  "Invalid task": "任务参数无效",
  "Input context must be a JSON object": "输入上下文必须是 JSON 对象",
  "Task key (optional)": "任务键（可选）",
  "What should the agent do? Point to slot/doc keys instead of pasting raw logs.":
    "Agent 需要做什么？请指向状态槽/文档键，而不是粘贴原始日志。",
  "Create task": "创建任务",
  Executor: "执行者",
  Analyst: "分析师",
  Auditor: "审计员",
  Scout: "侦察员",
  "Description": "描述",
  "Input context (JSON)": "输入上下文（JSON）",
  "No notifications": "没有通知",
  "No events": "没有事件",
  "No tasks yet": "还没有任务",
  "Unknown": "未知",
  "never": "从未",
  "just now": "刚刚",
  "unknown": "未知",
  "m ago": "分钟前",
  "h ago": "小时前",
  "d ago": "天前",
  // Governance
  "Reindex retrieval": "重建检索索引",
  "Governance sections": "治理分区",
  Proposals: "提案",
  Policies: "策略",
  Operations: "运维",
  "Access & budget": "访问与预算",
  "No proposals yet": "还没有提案",
  "Agents can submit a proposal with POST /v1/documents/{doc_key}/proposals.":
    "Agent 可通过 POST /v1/documents/{doc_key}/proposals 提交提案。",
  "Rebase conflict — review the latest document before applying.":
    "Rebase 冲突——应用前请检查最新文档。",
  Review: "审核",
  Apply: "应用",
  "New policy": "新建策略",
  "Policies match resource keys with glob patterns. A require_proposal path rejects direct agent writes.":
    "策略用 glob 匹配资源键。带 require_proposal 的路径会拒绝 Agent 直接写入。",
  "No path policies": "没有路径策略",
  "Writes are currently unrestricted by path. Add a policy to require review on sensitive namespaces.":
    "当前写入未按路径限制。可为敏感命名空间添加需要审核的策略。",
  "proposal required": "需要提案",
  disabled: "已禁用",
  "max {bytes} bytes": "最大 {bytes} 字节",
  Delete: "删除",
  "New rule": "新建规则",
  "Signed, persistent, retried deliveries from the outbox.":
    "来自 outbox 的签名、持久化、可重试投递。",
  "No webhook rules": "没有 Webhook 规则",
  "Add an endpoint to receive signed workspace events.":
    "添加一个端点以接收签名的工作区事件。",
  Test: "测试",
  "External actions": "外部动作",
  "Agent-proposed side effects wait for human approval; compensations are explicit.":
    "Agent 提议的副作用等待人工批准；补偿是显式的。",
  "No external actions": "没有外部动作",
  "Agents propose them with POST /v1/external-actions.":
    "Agent 通过 POST /v1/external-actions 提议。",
  Compensate: "补偿",
  Budget: "预算",
  "Monthly token/cost envelopes and per-minute request ceilings.":
    "月度 token/成本包络与每分钟请求上限。",
  "Tokens used (30d)": "已用 Token（30 天）",
  "Cost used (30d)": "已用成本（30 天）",
  "Request limit": "请求上限",
  "limit {value}": "上限 {value}",
  "fixed window": "固定窗口",
  "Monthly token budget": "月度 Token 预算",
  "Monthly cost budget (USD)": "月度成本预算（USD）",
  "Requests per minute": "每分钟请求数",
  "Save budget": "保存预算",
  "Human accounts": "人类账号",
  "OWNER can administer; REVIEWER can approve; VIEWER is read-only.":
    "OWNER 可管理；REVIEWER 可审批；VIEWER 只读。",
  "New account": "新建账号",
  "No human accounts": "没有人类账号",
  "Create an OWNER account before using the human console API.":
    "使用人类控制台 API 前，请先创建 OWNER 账号。",
  "last login": "上次登录",
  "Review note": "审核备注",
  "Apply proposal": "应用提案",
  "Applying uses three-way rebase; overlapping edits return a conflict instead of silently overwriting.":
    "应用时使用三方 rebase；重叠编辑会返回冲突，而不是静默覆盖。",
  "The most specific matching active policy wins.":
    "最具体且启用的匹配策略优先。",
  "Resource type": "资源类型",
  "Glob pattern": "Glob 模式",
  "Max content bytes (optional)": "最大内容字节（可选）",
  "Require a reviewed proposal for direct agent writes":
    "Agent 直接写入需要经过审核的提案",
  "Frontmatter JSON Schema": "Frontmatter JSON Schema",
  "Create policy": "创建策略",
  "New webhook rule": "新建 Webhook 规则",
  "Deliveries are signed with HMAC-SHA256 and retried with backoff.":
    "投递使用 HMAC-SHA256 签名，并按退避策略重试。",
  "HTTPS endpoint": "HTTPS 端点",
  "Event types (comma-separated)": "事件类型（逗号分隔）",
  "Create rule": "创建规则",
  "New human account": "新建人类账号",
  "The temporary password is shown once.": "临时密码只显示一次。",
  "Display name": "显示名称",
  "Create account": "创建账号",
  "Sign in": "登录",
  "Sign out": "退出登录",
  "Account access": "账户访问",
  Email: "邮箱",
  Password: "密码",
  "At least 8 characters": "至少 8 个字符",
  "Your password": "你的密码",
  "Your name": "你的名字",
  "Workspace name": "工作区名称",
  "Ada Lovelace": "你的名字",
  "My Agent Workspace": "我的 Agent 工作区",
  "Please wait…": "请稍候…",
  or: "或",
  "Operators can connect with the admin token in the sidebar.":
    "运维人员仍可在侧栏使用管理员令牌连接。",
  "Your session expired. Sign in again.": "登录已过期，请重新登录。",
  "Signed in as {email}.": "已登录：{email}",
  "Workspace created. Welcome to AgentGraph.": "工作区已创建，欢迎使用 AgentGraph。",
  "Unable to sign in.": "无法登录。",
  "Google sign-in failed.": "Google 登录失败。",
  "Human-in-the-loop control plane": "人机共驾控制平面",
  "The shared blackboard for agent teams: versioned truth, lease-fenced work, and human approval before anything risky happens.":
    "Agent 团队的共享黑板：版本化事实、租约围栏，以及风险动作前的人类批准。",
  "Approve the risky move": "批准风险动作",
  "Human gates hold escalations until a person decides.": "人工门禁会一直拦住升级任务，直到有人做决定。",
  "Never lose the thread": "永远不丢线索",
  "Every document version, lease and handoff is auditable.": "每个文档版本、租约和交接都可审计。",
  "Bring any runtime": "接入任意运行时",
  "MCP-native tools meet your agents where they already work.": "MCP 原生工具直接接入 Agent 现有的工作方式。",
  "Platform capabilities": "平台能力",
  "PostgreSQL 16 + pgvector": "PostgreSQL 16 + pgvector",
  "Lease fencing": "租约围栏",
  "Immutable versions": "不可变版本",
  "Welcome back": "欢迎回来",
  "Start your workspace": "创建你的工作区",
  "Sign in to AgentGraph": "登录 AgentGraph",
  "Create your account": "创建账号",
  "Your workspace is waiting on the other side.": "你的工作区正在另一边等你。",
  "A free workspace, ready for your first agent team.": "免费工作区，准备好迎接你的第一支 Agent 团队。",
  "Create free workspace": "创建免费工作区",
  "Enter workspace": "进入工作区",
  "Free workspace · no card required": "免费工作区 · 无需信用卡",
  "By continuing, you agree to the workspace terms and acceptable-use policy.":
    "继续即表示你同意工作区条款和可接受使用政策。",
  "Operator access": "运维入口",
  "External image blocked": "外部图片已阻止加载",
  "Load more": "加载更多",
  "Loaded {loaded} / {total}": "已加载 {loaded} / {total}",
  "Loaded {loaded} / {total} documents": "已加载 {loaded} / {total} 篇文档",
  "Unable to load more documents.": "无法加载更多文档。",
  "Unable to load audit history.": "无法加载审计历史。",
  "Verification status": "核验状态",
  "All statuses": "全部状态",
  "Taint state": "污点状态",
  "Any taint": "全部污点状态",
  "Clean": "无污点",
  "Sort order": "排序",
  "Newest first": "最新优先",
  "Oldest first": "最旧优先",
  "Updated after": "更新时间起",
  "Updated before": "更新时间止",
  "Unable to load documents.": "无法加载文档。",
  "Document namespaces": "文档命名空间",
  "Live counts by namespace, including archived-free documents.": "按命名空间统计的实时文档数（不含归档）。",
  "User sign in": "用户登录",
  "Deployment operator": "部署运维",
  "Control-plane access": "控制平面入口",
  "This door is for the operator of this AgentGraph deployment. It is separate from customer workspaces and never appears on the sign-in page.":
    "这是本 AgentGraph 部署运维人员的入口，与客户工作区分离，不会出现在用户登录页。",
  "The admin token stays in this browser.": "管理员令牌只保存在当前浏览器。",
  "It is never written to the repository or application logs.": "不会写入代码仓库或应用日志。",
  "Customer accounts use email or Google sign-in.": "客户账号使用邮箱或 Google 登录。",
  "Operator console": "运维控制台",
  "Connect the control plane": "连接控制平面",
  "Enter the API endpoint and the server's ADMIN_TOKEN.": "填写 API 地址和服务器 ADMIN_TOKEN。",
  "Enter control plane": "进入控制平面",
  "Only use this console on a trusted machine. Rotate the admin token if it was ever shared.":
    "请只在可信设备上使用控制台。如果令牌曾外传，请立即轮换。",
  "Looking for your workspace?": "想进入你的工作区？",
  "Connection settings": "连接设置",
  "Operator session": "运维会话",
  "Account created": "账号已创建",
  "Copy the temporary password now; it cannot be recovered.":
    "立即复制临时密码；之后无法找回。",
  "Stored securely": "已安全保存",
  "Operation failed.": "操作失败。",
  "Path policy created.": "路径策略已创建。",
  "Policy deleted.": "策略已删除。",
  "Webhook rule created.": "Webhook 规则已创建。",
  "Webhook test failed.": "Webhook 测试失败。",
  "Webhook delivered (HTTP {status}).": "Webhook 已投递（HTTP {status}）。",
  "Webhook deleted.": "Webhook 已删除。",
  "External action approved for dispatch.": "外部动作已批准，等待分发。",
  "External action rejected.": "外部动作已拒绝。",
  "Compensation queued.": "补偿已排队。",
  "Budget limits saved.": "预算限制已保存。",
  "Human account created; share the temporary password once.":
    "人类账号已创建；请只分享一次临时密码。",
  "Rebuilt {count} retrieval chunk(s).": "已重建 {count} 个检索分块。",
  "Policy {name} {state}.": "策略 {name} 已{state}。",
  "state enabled": "启用",
  "state disabled": "禁用",
  "Reject this proposal?": "驳回这个提案？",
  "Reject proposal": "驳回提案",
  "Review {docKey}": "审核 {docKey}",
  "base v{version}": "基线 v{version}",
  "{count} vote(s)": "{count} 票",
  "score {score}": "评分 {score}",
  "by {actor} · {time}": "由 {actor} · {time}",
  "Delete policy “{name}”?": "删除策略“{name}”？",
  "Delete webhook “{name}”?": "删除 Webhook“{name}”？",
  "Rejected by human": "人工拒绝",
  "Tokens used": "已用 Token",
  "Cost used": "已用成本",
  "No proposals": "没有提案",
  "No policies": "没有策略",
  "No webhooks": "没有 Webhook",
  "No actions": "没有动作",
  "No users": "没有用户",
  "Enabled": "已启用",
  "Paused": "已暂停",
  "pending": "待处理",
  "Arena": "竞技场",
  "single": "单提案",
  // Status labels
  TODO: "待办",
  READY: "就绪",
  IN_PROGRESS: "进行中",
  REVIEWING: "审核中",
  HANDOFF: "交接中",
  AWAITING_HUMAN: "等待人工",
  ESCALATED_TO_HUMAN: "已升级人工",
  DONE: "完成",
  FAILED: "失败",
  PENDING: "待处理",
  PENDING_APPROVAL: "待批准",
  APPROVED: "已批准",
  EXECUTING: "执行中",
  SUCCEEDED: "成功",
  REJECTED: "已拒绝",
  APPLIED: "已应用",
  SUPERSEDED: "已被取代",
  COMPENSATION_PENDING: "补偿待处理",
  COMPENSATED: "已补偿",
  DRAFT: "草稿",
  PENDING_REVIEW: "待审核",
  VERIFIED: "已验证",
  DEPRECATED: "已废弃",
  ACTIVE: "启用",
  SCOUT: "侦察员",
  ANALYST: "分析师",
  AUDITOR: "审计员",
  EXECUTOR: "执行者",
  COMMANDER: "指挥官",
  VIEWER: "只读",
  ANY: "任意",
  // Relative time
  "{count}m ago": "{count} 分钟前",
  "{count}h ago": "{count} 小时前",
  "{count}d ago": "{count} 天前",
  "Scope": "范围",
  "Actions": "动作",
  "Details": "详情",
  "Close": "关闭",
  "Cancel": "取消",
  "Save": "保存",
  "Loading…": "加载中…",
  "No data": "没有数据",
  "$5": "$5",
  "A doc_key is a stable path, e.g. strategy/btc_weekly_plan.": "doc_key 是稳定路径，例如 strategy/btc_weekly_plan。",
  "Agent token for {name} minted.": "已为 {name} 签发 Agent 令牌。",
  "Agents": "Agent",
  "Audit": "审计",
  "Blackboard": "黑板",
  "Close dialog": "关闭对话框",
  "Create a task for an agent role. Agents can also create next tasks during handoff.": "为某个 Agent 角色创建任务。Agent 也可在交接时创建后续任务。",
  "Created by": "创建者",
  "Document {doc} created.": "文档 {doc} 已创建。",
  "Document {doc} saved as v{version}.": "文档 {doc} 已保存为 v{version}。",
  "Documents": "文档",
  "Event": "事件",
  "Force": "强制",
  "Human": "人工",
  "Human locked": "人工锁定",
  "JSON Schema": "JSON Schema",
  "Language": "语言",
  "Lock this document against agent writes": "锁定此文档，阻止 Agent 写入",
  "New path policy": "新建路径策略",
  "Open L2 artifact": "打开 L2 产物",
  "Proposal for {doc} applied.": "提案 {doc} 已应用。",
  "Proposal for {doc} rejected.": "提案 {doc} 已驳回。",
  "Review proposals, gate write paths, sign webhooks, approve outbound actions and keep budgets observable.": "审核提案、控制写入路径、签名 Webhook、审批外发动作并保持预算可见。",
  "Review {doc}": "审核 {doc}",
  "Save human edit": "保存人工编辑",
  "Scout tokens should use a restricted namespace such as": "Scout 令牌应使用受限命名空间，例如",
  "Sign in with": "登录邮箱",
  "Tasks": "任务",
  "Timeline events were preserved.": "时间线事件已保留。",
  "Undid {count} record(s). Undo batch {batch}.": "已撤销 {count} 条记录。撤销批次 {batch}。",
  "Undo": "撤销",
  "Unlock": "解锁",
  "Verify": "验证",
  "Version history · {doc}": "版本历史 · {doc}",
  "Webhooks": "Webhooks",
  "active": "启用",
  "after": "后",
  "all": "全部",
  "before": "前",
  "by": "由",
  "content change": "内容变更",
  "document": "文档",
  "enabled": "启用",
  "live": "有效",
  "ns:": "命名空间：",
  "paused": "已暂停",
  "priority": "优先级",
  "risk flagged": "已标记风险",
  "roles:": "角色：",
  "score {score} / {count} vote(s)": "评分 {score} / {count} 票",
  "slot": "状态槽",
  "tainted source": "污染来源",
  "task": "任务",
  "via POST /v1/auth/login, then use the returned bearer token on /v1/human endpoints.": "通过 POST /v1/auth/login 登录，然后在 /v1/human 端点使用返回的 Bearer 令牌。",
  "{attempts}/{max} attempts": "尝试 {attempts}/{max} 次",
  "{count} change": "{count} 项变更",
  "{count} changes": "{count} 项变更",
  "{count} documents · namespace tree": "{count} 篇文档 · 命名空间树",
  "{count} recent failure(s)": "最近 {count} 次失败",
  "{docs} docs · {tasks} tasks": "{docs} 篇文档 · {tasks} 个任务",
  "{name} {state}.": "{name} {state}。",
  "OWNER": "所有者",
  "REVIEWER": "审核者",
  "arena": "竞技场",
  "Append-only collector. Raw namespaces only; no state reads/writes.": "只追加采集。仅限原始命名空间；不读取/写入状态。",
  "Reads everything, writes documents and proposes tasks. No state writes.": "可读取全部内容，写文档并提议任务；不写状态。",
  "Reviews tasks, reads audit, can write review notes. No state writes.": "审核任务、读取审计并写审核备注；不写状态。",
  "Claims tasks and owns state writes. Human locks still override.": "认领任务并负责状态写入；人类锁仍然优先。",
  "HUMAN_OVERRIDE": "人工覆写",
  "TASK_ESCALATED": "任务升级",
  "TASK_REJECTED": "任务驳回",
  "BATCH_UNDONE": "批次撤销",
  "TASK_CREATED": "任务创建",
  "TASK_CLAIMED": "任务认领",
  "TASK_RELEASED": "任务释放",
  "TASK_HANDOFF": "任务交接",
  "TASK_APPROVED": "任务通过",
  "HUMAN_APPROVED": "人工通过",
  "HUMAN_REJECTED": "人工驳回",
  "TASK_READY": "任务就绪",
  "LEASE_EXPIRED": "租约过期",
  "DOC_CREATED": "文档创建",
  "DOC_UPDATED": "文档更新",
  "DOC_VERIFIED": "文档验证",
  "DOC_SECTION_PATCHED": "文档章节修改",
  "DOC_STATUS_CHANGED": "文档状态变更",
  "DOC_TAINT_CHANGED": "文档污点变更",
  "SLOT_CREATED": "状态槽创建",
  "SLOT_UPDATED": "状态槽更新",
  "SLOT_PATCHED": "状态槽修改",
  "PROPOSAL_CREATED": "提案创建",
  "PROPOSAL_VOTED": "提案投票",
  "PROPOSAL_APPLIED": "提案应用",
  "PROPOSAL_REJECTED": "提案驳回",
  "EXTERNAL_ACTION_CREATED": "外部动作创建",
  "EXTERNAL_ACTION_APPROVED": "外部动作批准",
  "EXTERNAL_ACTION_REJECTED": "外部动作拒绝",
  "EXTERNAL_ACTION_SUCCEEDED": "外部动作成功",
  "EXTERNAL_ACTION_FAILED": "外部动作失败",
  "EXTERNAL_COMPENSATION_SCHEDULED": "外部补偿已排队",
  "LINK_CREATED": "链接创建",
  "Dismiss error": "关闭错误提示",
  "Quant & Research Hub": "量化与研究中枢",
  "Validate BTC order-block liquidity": "验证 BTC 订单簿流动性",
  "Review feedback": "审核反馈",
  "Reviewer": "审核人",
};

const EN: Record<string, string> = {};

const I18nContext = createContext<{
  lang: Language;
  setLang: (language: Language) => void;
  t: (source: string, vars?: Record<string, string | number>) => string;
} | null>(null);

function interpolate(template: string, vars?: Record<string, string | number>) {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, key) =>
    Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : match,
  );
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Language>("en");
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const stored = window.localStorage.getItem("agentgraph.lang");
    const initial: Language =
      stored === "zh" || stored === "en"
        ? stored
        : navigator.language.toLowerCase().startsWith("zh")
          ? "zh"
          : "en";
    setLangState(initial);
    setFormatLanguage(initial);
    setHydrated(true);
  }, []);

  const setLang = useCallback((next: Language) => {
    setLangState(next);
    setFormatLanguage(next);
    window.localStorage.setItem("agentgraph.lang", next);
    document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang, hydrated]);

  const t = useCallback(
    (source: string, vars?: Record<string, string | number>) => {
      const template = lang === "zh" ? (ZH[source] ?? EN[source] ?? source) : source;
      return interpolate(template, vars);
    },
    [lang],
  );

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) throw new Error("useI18n must be used inside I18nProvider");
  return context;
}

export function LanguageToggle() {
  const { lang, setLang, t } = useI18n();
  return (
    <div className="language-toggle" role="group" aria-label={t("Language")}>
      <button
        type="button"
        className={lang === "en" ? "active" : ""}
        aria-pressed={lang === "en"}
        onClick={() => setLang("en")}
      >
        EN
      </button>
      <button
        type="button"
        className={lang === "zh" ? "active" : ""}
        aria-pressed={lang === "zh"}
        onClick={() => setLang("zh")}
      >
        中文
      </button>
    </div>
  );
}
