# QwenPaw · 火山引擎 Mem0 用户使用说明书

- 版本：v2.0（官方记忆后端版）
- 部署架构：官方记忆后端（`BaseMemoryManager`）＋ 备份同步 ＋ 启动激活补救
- 适用对象：QwenPaw 2.x（多 Agent）使用者

## 目录

1. [系统概述](#一系统概述)
2. [v1.0 → v2.0 变更说明](#二v10--v20-变更说明)
3. [部署与环境要求](#三部署与环境要求)
4. [快速上手指南](#四快速上手指南)
5. [核心功能详解](#五核心功能详解)
6. [记忆工具使用说明（5 个工具）](#六记忆工具使用说明5-个工具)
7. [费用控制与用量监控](#七费用控制与用量监控)
8. [常见问题与排查（FAQ）](#八常见问题与排查faq)
9. [后台数据查看与维护建议](#九后台数据查看与维护建议)

---

## 一、系统概述

### 1.1 系统定位

QwenPaw 通过插件 `mem0-integration` 接入火山引擎托管的 Mem0 记忆服务，为指定 Agent 提供跨会话的长期记忆能力。

v2.0 采用 QwenPaw 2.x 官方记忆后端接口（`BaseMemoryManager`），以「后端（backend）」形式接入，取代 v1.0 基于 AgentScope 类钩子的注入方案。后端按 Agent 独立启用：配置为 `mem0` 的 Agent 使用云端 Mem0，其余 Agent 继续使用本地 ReMe，互不影响。

### 1.2 核心特性

| 特性 | 说明 |
| --- | --- |
| 官方后端接入 | 通过 `api.register_memory_backend` 注册 `backend_id=mem0`，符合 QwenPaw 2.x 官方契约 |
| 按 Agent 隔离 | 以 `agent_id` 作为 Mem0 的 `user_id`，各 Agent 记忆互不可见 |
| 自动记忆闭环 | 回复前自动检索并注入；按间隔批量写入（默认每 5 轮） |
| 5 个记忆工具 | `search_memory` / `add_memory` / `list_memories` / `update_memory` / `delete_memory` |
| 降本四道闸门 | 批量写入、丢弃工具输出、噪音与长度过滤、内容去重 |
| 费用护栏 | `usage.json` 用量记账 ＋ 月度预算熔断（只停写、不停读） |
| 备份可恢复 | 同步到 default workspace 的 `_mem0_backup/`，随 QwenPaw 备份走 |
| 启动激活补救 | 解决「插件加载晚于工作区启动」的时序问题，零停机重载 |
| 零第三方记忆依赖 | 仅用 `httpx` 直连 Mem0 REST API，运行时不再依赖 `mem0ai` |

### 1.3 架构总览

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 插件入口 | `backend.py` | 注册记忆后端 ＋ 注册 4 个启动 / 关闭钩子 |
| 后端实现 | `mem0_service/memory_backend.py` | `Mem0MemoryManager`，实现官方 `BaseMemoryManager` |
| REST 客户端 | `mem0_service/http_client.py` | `httpx` 直连 Mem0 REST，令牌桶限流 ＋ 退避重试 |
| 工具实现 | `mem0_service/tools.py` | 5 个记忆工具的同步实现 |
| 过滤与去重 | `mem0_service/content_filter.py` | 噪音 / 长度过滤、工具消息丢弃、TTL 去重 |
| 用量与熔断 | `mem0_service/usage_meter.py` | 用量记账与月度预算熔断 |
| 配置 | `mem0_service/config.py` | 自实现 `.env` 解析 ＋ 不可变配置单例 |
| 激活补救 | `mem0_service/activation.py` | 启动后三步激活与真实实例复核 |

运行时数据流：

```text
QwenPaw 启动
  └─ 插件 backend.py register()
       ├─ api.register_memory_backend(backend_id="mem0", factory=Mem0MemoryManager)
       └─ 注册钩子：restore(5) / backup(8) / shutdown(5) / activate(90)

对话请求（配置为 mem0 的 Agent）
  ├─ 回复前：基类 auto_memory_search() → memory_search()
  │            → POST /v1/memories/search/ → 注入 [用户长期记忆]
  ├─ 生成回答
  └─ 每 5 轮：auto_memory() → 过滤/去重/熔断 → POST /v1/memories/

启动后约 20 秒
  └─ activation.py：reserve_selection → reload_agent → 真实实例复核
```

---

## 二、v1.0 → v2.0 变更说明

v2.0 是被迫也是彻底的架构升级：QwenPaw 2.x 所用 agentscope 2.0.7 已移除 `register_class_hook`、`agentscope.memory`、`_MemoryMark`，v1.0 的类钩子注入路径整体失效，因此改为对接官方记忆后端接口。

| 维度 | v1.0（已失效） | v2.0（当前） |
| --- | --- | --- |
| 接入方式 | AgentScope 类钩子注入 | 官方记忆后端 `api.register_memory_backend` |
| 生效范围 | 钩子全局注入 | `agent.json` 中 `running.memory_manager_backend`，按 Agent |
| 自动检索 | `pre_reply` 钩子 | 基类 `auto_memory_search()` ＋ `memory_search()` |
| 自动写入 | `post_reply` 钩子 ＋ 自建异步队列 | 基类 `submit_auto_memory` 队列 ＋ `auto_memory()`（默认 5 轮） |
| 记忆工具 | 5 个 Skill（add/search/get/update/delete） | 5 个工具（search/add/list/update/delete），随后端注册 |
| 第三方依赖 | `mem0ai` 0.1.118、`python-dotenv`、`fastapi` 等 | 仅 `httpx`（已内置），运行时零 `mem0ai` |
| 服务地址 | mem0ai SDK 封装 | `httpx` 直连专属实例 REST 接入点 |
| 降本 / 用量 | 无 | 四道闸门 ＋ `usage.json` 记账 ＋ 月度预算熔断 |
| 启动激活 | 不涉及 | `activation.py` 三步补救（reserve → reload → 复核） |

> 结论：v1.0 说明书中的「Hook 兜底 ＋ Skill 增强」描述已全部作废，请以本 v2.0 说明书为准。

---

## 三、部署与环境要求

### 3.1 前置条件

| 项目 | 要求 |
| --- | --- |
| QwenPaw | 2.x 桌面版（PyInstaller 冻结包） |
| 火山引擎 Mem0 | 已创建实例，具备「公网连接地址」与 API Key |
| 插件目录 | `%USERPROFILE%\.qwenpaw\plugins\mem0-integration` |
| Python 环境 | 使用 QwenPaw 自带 Python 即可，无需另行安装依赖 |
| 操作系统 | Windows（开关脚本为 PowerShell；核心代码跨平台） |

### 3.2 插件目录结构

```text
mem0-integration/
  ├─ plugin.json             插件清单（version=2.0.0，entry.backend=backend.py）
  ├─ backend.py              插件入口：注册后端 + 4 个钩子
  ├─ set_mem0_agent.ps1      按 Agent 开关 Mem0 的脚本
  ├─ requirements.txt        运行依赖（仅 httpx）
  └─ mem0_service/
       ├─ memory_backend.py  Mem0MemoryManager（官方后端实现）
       ├─ http_client.py     Mem0 REST 客户端（httpx，令牌桶）
       ├─ tools.py           5 个工具实现
       ├─ content_filter.py  噪音/长度过滤 + 去重
       ├─ usage_meter.py     用量记账 + 预算熔断
       ├─ activation.py      启动激活补救
       ├─ config.py          .env 解析 + 配置单例
       └─ .env.example       环境变量模板（复制为 .env 后填写）
```

> `plugin.log`、`backend.log`、`usage.json`、`__pycache__/`、`_mem0_backup/` 均为**运行时本地生成**，已被 `.gitignore` 排除，不随仓库分发。

### 3.3 依赖说明

v2.0 运行时不依赖 `mem0ai`。Mem0 调用全部由 `http_client.py` 用 `httpx` 直连 REST 完成，`httpx` 已随 QwenPaw 后端内置，一般无需安装；独立环境可执行仓库根目录的 `pip install -r requirements.txt`（内容仅 `httpx`）。

| 组件 | v1.0 | v2.0 实际 |
| --- | --- | --- |
| mem0ai | 必需 | 不再使用（httpx 直连 REST） |
| httpx | 间接依赖 | 直接使用（已内置） |
| python-dotenv | 使用 | 不使用（`config.py` 自实现 `.env` 解析） |
| pydantic / fastapi / uvicorn | 使用 | 不使用 |

### 3.4 服务地址与凭据（`mem0_service/.env`）

从 `.env.example` 复制为 `.env` 后填写：

| 配置键 | 取值 | 说明 |
| --- | --- | --- |
| `VOLC_MEM0_HOST` | `https://mem0-<your-instance-id>.mem0.volces.com:8000` | 控制台「接入点」里的专属公网连接地址 |
| `VOLC_MEM0_API_KEY` | `<your-api-key>` | 控制台「API Key 管理」获取，形如 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `MEM0_QPS_LIMIT` | `18` | 客户端限流上限（火山引擎硬限 20 QPS） |
| `MEM0_REQUEST_TIMEOUT` | `5` | 连接超时（秒） |
| `MEM0_READ_TIMEOUT` | `15` | 读取超时（秒，默认值；网络较慢可按需上调，追求快速失败可下调） |
| `MEM0_BLACKLIST_TTL` | `900` | 去重缓存 / 删除防复活黑名单 TTL（秒） |

> `.env` 中若残留 `MEM0_SERVICE_HOST` / `MEM0_SERVICE_PORT` / `MEM0_QUEUE_MAX_SIZE` 等 v1.0 旧键，v2.0 不再读取，可忽略或删除。

---

## 四、快速上手指南

### 4.1 首次配置

1. 将插件放入 `%USERPROFILE%\.qwenpaw\plugins\mem0-integration`；
2. 复制 `mem0_service\.env.example` 为 `mem0_service\.env`，填入 `VOLC_MEM0_HOST` 与 `VOLC_MEM0_API_KEY`；
3. 重启 QwenPaw。

### 4.2 为 Agent 开启 / 关闭 Mem0

使用插件目录下的 `set_mem0_agent.ps1`（原理：修改对应 workspace 的 `agent.json` 中 `running.memory_manager_backend` 字段）：

```powershell
# 查看各 Agent 当前后端
.\set_mem0_agent.ps1 -List

# 为 default 开启云端 Mem0
.\set_mem0_agent.ps1 -AgentId default

# 为某个 Agent 关闭云端 Mem0（改回本地 ReMe），以下 agent 名仅为示例
.\set_mem0_agent.ps1 -AgentId your_agent -Backend remelight
```

取值：`mem0` = 火山引擎云端 Mem0（计费）；`remelight` = 本地 ReMe（免费）。

安全性：写入前自动备份到 `~/.qwenpaw/workspaces/_backend_switch_backups/`，替换前校验「仅 1 处匹配」，替换后校验 JSON 合法性。修改后需重启 QwenPaw 生效。

### 4.3 Agent 后端状态（示例）

`-List` 会列出每个 Agent 当前的记忆后端，例如：

| Agent（示例） | 当前后端 | 含义 |
| --- | --- | --- |
| `default` | mem0 | 火山引擎 Mem0（云端） |
| `your_agent` | mem0 | 火山引擎 Mem0（云端） |
| `another_agent` | remelight | 本地 ReMe（免费） |

### 4.4 启动与激活

QwenPaw 的 Agent 工作区会先于插件加载启动，因此配置为 mem0 的 Agent 首次可能回退到 remelight。插件在启动后约 20 秒自动执行一次零停机重载完成激活，无需人工干预。

| 步骤 | 调用 | 作用 |
| --- | --- | --- |
| 1 | `memory_registry.reserve_selection("mem0", agent_id)` | 登记「预留」，使配置在重载时被采纳（本身不创建实例） |
| 2 | `MultiAgentManager.reload_agent(agent_id)` | 零停机重载工作区，此时预留生效、后端被实例化 |
| 3 | 真实实例复核 | 以 `isinstance` 扫描为准，确认 `Mem0MemoryManager` 实例存在 |

成功判据：`backend.log` 出现：

```text
✓ 激活成功，真实实例: ['default', 'your_agent']
```

### 4.5 验证是否生效

1. 查看 `mem0_service\backend.log`，确认出现「Mem0 连接成功」与「✓ 激活成功」；
2. 在会话中陈述一条个人事实（例如「我喜欢吃蔬菜」），等待约 1-3 分钟；
3. 新建会话提问「我喜欢吃什么」，若能回忆则生效；也可直接调用 `list_memories` 工具盘点。

---

## 五、核心功能详解

### 5.1 自动检索注入

基类 `auto_memory_search()` 负责编排：调用 `memory_search(query, max_results)`，命中后把结果包装为合成工具消息注入模型上下文；无命中时返回框架约定的 `NO_RELEVANT_MEMORIES`（`No relevant memories found.`），跳过注入。默认返回条数由 `MEM0_MAX_RESULTS` 控制，默认 3。

### 5.2 自动批量写入

框架的 `submit_auto_memory` 队列按 `get_auto_memory_interval()` 触发，默认每 5 轮用户回复批量写入一次。`auto_memory()` 的执行顺序：

1. 移除框架自动检索注入的合成消息（避免把「检索动作」写进记忆）；
2. 归一化为 `{role, content}`；
3. 丢弃 tool 角色消息（降本）；
4. 噪音 / 长度过滤（`should_write`）；
5. 内容去重（`DedupCache`）；
6. 费用熔断检查（`is_write_allowed`）；
7. 调用 `POST /v1/memories/` 写入，`metadata.source = auto_memory`。

### 5.3 记忆隔离

写入与检索均以 `agent_id` 作为 Mem0 的 `user_id`，因此每个 Agent 拥有独立记忆空间，互不可见。

### 5.4 降本四道闸门

| 闸门 | 机制 | 默认参数 | 作用 |
| --- | --- | --- | --- |
| 1 批量写入 | 每 N 轮写一次 | `MEM0_AUTO_MEMORY_INTERVAL=5` | 摊薄服务端抽取的固定开销 |
| 2 丢弃工具输出 | `strip_tool_messages` | `MEM0_KEEP_TOOL_MESSAGES=False` | 避免数万字符工具输出计入抽取 token |
| 3 噪音 / 长度过滤 | `is_noise` / `should_write` | `MEM0_MIN_WRITE_LENGTH=10` | 寒暄、报错回显、心跳不触发 LLM 抽取 |
| 4 内容去重 | `DedupCache` | `MEM0_BLACKLIST_TTL=900` | 同内容在 TTL 内不重复写入 |

### 5.5 用量记账与预算熔断

火山引擎 Mem0 自 **2026-11-02 10:00（UTC+8）** 起计费：Credit 消耗 1.12 元 / 百万 Credit（LLM 输入 / 输出 ＋ Embedding token，主要成本项）；记忆存储 0.0025 元 / 万条·小时。

`usage.json` 按月分桶记录 `calls` / `tokens` / `credits` / `yuan` 与各调用类型明细。换算口径（**估算，非服务端真实计量**）：

```text
tokens  ≈ 字符数 / 1.6（可被框架 token_estimate_divisor 覆盖）
credits = tokens × 3.2            （MEM0_CREDIT_PER_TOKEN）
yuan    = credits / 1,000,000 × 1.12（MEM0_YUAN_PER_MILLION_CREDIT）
```

当本月估算金额达到 `MEM0_MONTHLY_BUDGET_YUAN`（默认 10 元）时，仅暂停「写入」，检索照常放行（读远比写便宜）。

---

## 六、记忆工具使用说明（5 个工具）

5 个工具随后端注册，Agent 可在需要时显式调用；也可在会话中直接以自然语言要求 Agent 使用它们。

| 工具 | 参数 | 作用 | 备注 |
| --- | --- | --- | --- |
| `search_memory` | `query`, `limit=5` | 语义检索长期记忆 | 结果含记忆 id 与相似度 |
| `add_memory` | `content`, `tags=""` | 显式写入一条长期记忆 | tags 用逗号分隔；异步落库，约 1-3 分钟可检索 |
| `list_memories` | `limit=20` | 列出现有记忆 | 用于盘点或获取 memory_id |
| `update_memory` | `memory_id`, `content` | 修改一条记忆内容 | 建议先用 search / list 拿到 id |
| `delete_memory` | `memory_id` | 删除一条记忆 | 自动加入防复活黑名单，避免被异步队列写回 |

对话示例：

- 「回忆一下我之前说过喜欢吃什么」→ 触发 `search_memory`
- 「记住：我的项目代号是 QwenPaw」→ 触发 `add_memory`
- 「列出你记住的关于我的信息」→ 触发 `list_memories`

---

## 七、费用控制与用量监控

### 7.1 可调参数（`mem0_service/.env`）

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `MEM0_AUTO_MEMORY_INTERVAL` | `5` | 自动写入间隔（`0` = 禁用自动写入） |
| `MEM0_MAX_RESULTS` | `3` | 自动检索返回条数 |
| `MEM0_MIN_WRITE_LENGTH` | `10` | 写入最小长度门槛 |
| `MEM0_KEEP_TOOL_MESSAGES` | `False` | 是否保留工具消息（`True` 会显著增加成本） |
| `MEM0_MEMORY_SEARCH_ENABLED` | `True` | 是否启用自动检索注入 |
| `MEM0_USAGE_ENABLED` | `True` | 是否启用用量记账 |
| `MEM0_MONTHLY_BUDGET_YUAN` | `10.0` | 月度预算上限（元），超出只停写 |
| `MEM0_CREDIT_PER_TOKEN` | `3.2` | Credit / token 换算系数（建议按首个计费月账单校准） |
| `MEM0_YUAN_PER_MILLION_CREDIT` | `1.12` | 元 / 百万 Credit |
| `MEM0_BLACKLIST_TTL` | `900` | 去重与防复活黑名单 TTL（秒） |

### 7.2 调参建议

| 目标 | 建议 |
| --- | --- |
| 进一步降本 | 提高 `MEM0_AUTO_MEMORY_INTERVAL`（如 10）、降低 `MEM0_MAX_RESULTS`（如 2） |
| 只保留手动写入 | 将 `MEM0_AUTO_MEMORY_INTERVAL` 设为 `0`，仅用 `add_memory` 显式写入 |
| 临时关闭自动检索 | `MEM0_MEMORY_SEARCH_ENABLED=False`（仍可手动 `search_memory`） |
| 放宽预算 | 调高 `MEM0_MONTHLY_BUDGET_YUAN` |

---

## 八、常见问题与排查（FAQ）

**Q1：记忆没生效 / 后台查不到数据？**
确认该 Agent 的 `memory_manager_backend` 为 `mem0`（`set_mem0_agent.ps1 -List`）；查看 `backend.log` 是否出现「Mem0 连接成功」和「✓ 激活成功」。服务端 `add` 为异步落库，写入后约 1-3 分钟才可检索，属正常。

**Q2：日志显示配置为 mem0 却回退 remelight？**
这是插件加载晚于工作区启动的时序现象。插件会在启动后约 20 秒自动重载激活；若仍失败，可在 QwenPaw 界面手动重载该 Agent，或查看 `backend.log` 的激活诊断。

**Q3：启动日志出现「✓ 激活成功，真实实例: ['?']」？**
这是旧版激活模块的假警报，根因是读取了错误的实例属性名（旧代码用 `_agent_id`，基类实际为 `agent_id`）。当前版本已修复为读取 `agent_id`，不会再出现 `['?']`；若仍复现，请确认 `activation.py` 已更新到最新版本。

**Q4：`/new` 或 `/compact` 崩溃：`UNKNOWN_AGENT_ERROR ... _auto_memory_worker_stopping`？**
原因是 `Mem0MemoryManager` 构造时未调用基类 `super().__init__(context=context)`，导致基类 worker 队列等共享状态缺失。当前版本已修复（构造中显式调用基类初始化）；若升级后仍出现，请检查 `memory_backend.py` 的 `__init__`。

**Q5：出现 `PermissionError: [WinError 5]`？**
该错误并非代码缺陷，而是安全软件（如 360 安全卫士实时防护）拦截了插件对文件的写入。请在安全软件中将 `%USERPROFILE%\.qwenpaw` 加入信任区后重启 QwenPaw。

**Q6：写入成功但检索不到？**
等待 1-3 分钟（异步落库）；确认该内容通过了过滤（过短、纯寒暄、报错回显、心跳类内容会被刻意跳过）；确认未因去重被跳过（同一内容在 TTL 内只写一次）。

**Q7：提示「已达月度预算上限，已暂停写入」？**
本月估算金额已达到 `MEM0_MONTHLY_BUDGET_YUAN`。检索不受影响；如需继续写入，请调高预算，或等待次月自然重置。

**Q8：换机 / 重装后如何恢复？**
插件会把自身内容同步到 default workspace 的 `_mem0_backup/` 目录，随 QwenPaw 备份一并迁移。新环境中若插件目录缺少 `.env`，启动钩子会自动从该备份目录恢复代码与配置（日志显示「[restore] 恢复完成」）。注意 `usage.json` 等本地状态文件不参与备份。

**Q9：API Key 泄露风险？**
`.env` 内含 API Key，请勿外发该文件，仓库默认通过 `.gitignore` 排除它。如疑似泄露，请在火山引擎控制台重置 Key 并同步更新 `.env`。

---

## 九、后台数据查看与维护建议

### 9.1 火山引擎控制台查看

| 查看项 | 位置 |
| --- | --- |
| 记忆条目 | Mem0 控制台 → 记忆（Memories）列表 |
| 调用与用量 | 控制台 → 用量 / 账单 |
| 计费规则 | 控制台 → 计费说明（自 2026-11-02 起） |

### 9.2 日志与数据文件

| 文件 | 路径 | 用途 |
| --- | --- | --- |
| `backend.log` | `mem0_service\backend.log` | 后端连接、写入、激活诊断 |
| `plugin.log` | 插件根目录 `plugin.log` | 插件注册与钩子执行 |
| `usage.json` | `mem0_service\usage.json` | 本地用量记账（按月分桶） |
| `_mem0_backup\` | default workspace 下 | 部署备份（随 QwenPaw 备份） |

### 9.3 维护频次

| 周期 | 动作 |
| --- | --- |
| 每次重启后 | 确认 `backend.log` 出现「✓ 激活成功」 |
| 每月初 | 核对 `usage.json` 估算值与控制台账单，必要时校准换算系数 |
| 计费首月后 | 依据真实账单校准 `MEM0_CREDIT_PER_TOKEN` |
| 按需 | 用 `list_memories` / `delete_memory` 清理无效记忆 |
| 升级 QwenPaw 后 | 确认激活仍成功；如接口变更，参考官方 `plugins/memory` 参考实现调整 |

---

— 文档结束 —
