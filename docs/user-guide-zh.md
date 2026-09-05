# QwenPaw · 火山引擎 Mem0 用户使用说明书

- 版本：v1.1
- 部署架构：Hook 兜底 + Skill 增强

> 路径约定：`~` 代表当前用户主目录。Windows 下 `~/.qwenpaw` 即「用户主目录下的 `.qwenpaw`」；`<QwenPaw安装目录>` 代表 QwenPaw 的实际安装路径。
> 架构总览、设计决策与独立 FastAPI 运行方式见仓库根目录 [README.md](../README.md)，本手册聚焦在 QwenPaw 内的实际使用与排障。

## 目录

1. 系统概述（1.1 系统定位 1.2 核心特性 1.3 架构总览）
2. 部署与环境要求（2.1 前置条件 2.2 目录结构 2.3 版本清单）
3. 快速上手指南（3.1 启动验证 3.2 写入第一条记忆 3.3 验证记忆召回）
4. 核心功能详解（4.1 Hook 自动记忆层 4.2 Skill 显式工具层 4.3 备份与升级保障）
5. 5 个 Skill 工具使用说明
6. 常见问题与排查
7. 后台数据查看（火山引擎控制台）
8. 维护与建议

## 一、系统概述

### 1.1 系统定位

本说明书介绍如何在 QwenPaw 桌面客户端中使用火山引擎（Volcengine）托管的 Mem0 记忆系统。Mem0 是一个为大语言模型（LLM）智能体设计的“长期记忆层”，它能够在多轮对话中自动提取、存储和检索用户偏好、事实与上下文，让每个智能体都“记住”之前的对话内容。

系统采用 “Hook 兜底 + Skill 增强” 双架构：底层 Hook 在每次对话前后自动触发记忆读写，无需用户操作；上层 Skill 向智能体暴露 5 个显式记忆工具，可在需要时精准调用。

### 1.2 核心特性

| 特性 | 说明 |
|---|---|
| 自动记忆 | 对话前自动检索相关历史，对话后异步写入新记忆，零配置启用。 |
| 智能体隔离 | 每个智能体（agent）以 name 作为 user_id，记忆完全独立不串扰。 |
| 异步写入队列 | 令牌桶限流（默认 18 QPS）+ 1000 条队列，避免阻塞对话，失败自动重试。 |
| 防复活黑名单 | 已删除记忆的内容及其变体写入带 TTL 的黑名单，防止异步回写意外恢复。 |
| 备份可恢复 | 插件目录同步至 workspace/_mem0_backup/，QwenPaw 备份功能完整覆盖。 |
| 升级兼容 | 所有代码位于用户级 ~/.qwenpaw/plugins/，QwenPaw 升级不覆盖。 |

### 1.3 架构总览

```
QwenPaw 客户端
  └─ Plugin 系统 ( ~/.qwenpaw/plugins/mem0-integration/ )
       ├─ startup/shutdown 钩子  ←→  备份同步
       ├─ backend.py  注入 MemoryHooks + 5 个 Skill 工具
       │    ├─ Hook 层：pre_reply / pre_reasoning / post_acting / post_reply
       │    └─ Skill 层：add / search / list / update / delete 记忆
       └─ MemoryCoordinator  →  异步队列 + 令牌桶限流 + 防复活黑名单
                   │
                   ▼
             MemoryClient (mem0ai 0.1.118)
                   │ HTTPS (api_key)
                   ▼
         火山引擎 Mem0 托管服务 (mem0-cn-beijing.volces.com)
```

> 另有一条可选旁路：`mem0_service/api/routes.py` 提供独立 FastAPI 服务（`/memories` REST 接口），不依赖 QwenPaw 进程，启动方式见 README「方式二」。

## 二、部署与环境要求

### 2.1 前置条件

| 条件 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 x64 |
| QwenPaw 版本 | 最新桌面版（原生启动：QwenPaw Desktop.vbs） |
| Python 环境 | QwenPaw 自带 Python 3.10（位于 `<QwenPaw安装目录>\python.exe`） |
| 火山引擎账号 | 已开通 Mem0 托管服务，创建项目并获取 API Key |
| 网络连通性 | 能够访问 https://mem0-cn-beijing.volces.com （443 端口） |

### 2.2 目录结构

所有部署文件位于 QwenPaw 用户数据目录，确保升级不丢失：

```
~/.qwenpaw/plugins/mem0-integration/
├── plugin.json              # 插件清单（id=mem0-integration, type=hook）
├── backend.py               # 插件入口：startup/shutdown 钩子 + 注入逻辑
├── test_plugin.py           # 注入逻辑自测（Path.home 定位，支持环境变量覆盖）
├── mem0_service\
│   ├── config.py            # Mem0 配置类（全部读环境变量，含校验）
│   ├── .env.example         # 环境变量模板（复制为 .env 填 Key，.env 不入库）
│   ├── api\routes.py        # 【可选】独立 FastAPI 服务
│   ├── utils\
│   │   └── coordinator.py   # MemoryCoordinator：队列 + 限流 + 黑名单
│   ├── hooks\
│   │   └── memory_hooks.py  # pre_reply/post_reply 等 4 个钩子
│   ├── skills\
│   │   └── memory_skills.py # 5 个记忆工具函数（ALL_TOOLS）
│   └── qwenpaw_client\
│       └── mem0_client.py   # mem0ai SDK 封装
└── requirements.txt         # 独立运行时的依赖汇总

~/.qwenpaw/workspaces/default/_mem0_backup/   # 自动同步备份
    └── _RESTORE_INFO.json   # 恢复元信息（时间戳 + 源路径）
```

### 2.3 版本清单

| 包名 | 版本 | 用途 |
|---|---|---|
| mem0ai | 0.1.118 | 使用 MemoryClient 对接托管 HTTP 服务 |
| agentscope | 1.0.20 | Toolkit / `register_class_hook` 机制（QwenPaw 自带） |
| python-dotenv | 1.0.1 | `.env` 配置文件加载 |
| pydantic | 2.10.4 | 工具入参运行时类型解析 |
| httpx | 0.28.1 | 异步 HTTP 客户端 |
| fastapi / uvicorn | 0.115.6 / 0.34.0 | 仅独立 FastAPI 模式需要 |

> 作为 QwenPaw 插件运行时，上述多数包已由宿主自带 Python 预装；在独立虚拟环境运行时，执行 `pip install -r requirements.txt` 一键安装。

## 三、快速上手指南

### 3.1 启动验证

1. 关闭 QwenPaw，等待 10 秒确保进程完全退出。
2. 双击桌面 “QwenPaw Desktop.vbs” 启动客户端（勿使用第三方启动器）。
3. 打开任意一个智能体，观察启动日志。若看到以下信息则说明插件加载成功：

```
[mem0-integration] startup hook triggered, injecting hooks & skills...
[MemoryCoordinator] worker thread started, queue maxsize=1000
```

若未看到日志，请执行 6.1 节排查。

### 3.2 写入第一条记忆

测试示例：“我喜欢吃蔬菜”。

在智能体对话框输入这句话并发送，系统会在 post_reply 钩子中自动将本轮对话写入 Mem0（异步队列，通常 1–3 秒内完成）。此记忆以当前智能体 name 作为 user_id 隔离。

### 3.3 验证记忆召回

1. 新建一个对话窗口（或清空当前上下文）。
2. 向同一个智能体提问：“我喜欢吃什么？”
3. 观察智能体是否回答 “蔬菜” 或相关内容。
   - 若回答正确 → Hook 自动记忆读写均正常。
   - 若回答不正确 → 请执行 6.3 节“记忆不召回”排查。

## 四、核心功能详解

### 4.1 Hook 自动记忆层（兜底层）

Hook 层通过 AgentScope 的 `register_class_hook` 机制在 Agent 类上注册 4 个生命周期钩子（类级别，对所有实例生效），对用户透明，无需任何显式操作。

| 钩子 | 注册名 | 触发时机 | 作用 |
|---|---|---|---|
| pre_reply | mem0_search_inject | 回复前（核心） | 提取最后一条用户消息，search() 召回 TOP 5 记忆，以 HINT 注入上下文 |
| pre_reasoning | mem0_search_fallback | 推理前（兜底） | 当 pre_reply 未命中时补一次检索，标志位去重 |
| post_acting | mem0_tool_collect | 工具执行后 | 累积工具结果到 buffer，供写入时合并 |
| post_reply | mem0_async_write | 回复后（核心） | 将本轮用户消息 + 回复 + 工具结果入队，异步 add() 写入 |

关键设计细节：

- **防心跳写入**：心跳 / ACK 类消息通过 `_is_heartbeat()` 判定后跳过，不产生噪音记忆。
- **user_id 隔离**：以 agent.name 作为 user_id，不同智能体记忆完全独立。
- **提示注入格式**：召回的记忆以 “【记忆检索结果】\n- …” 格式拼入 hint。

### 4.2 Skill 显式工具层（增强层）

Skill 层通过 AgentScope Toolkit 向智能体注册 5 个工具函数，智能体在需要时可以显式调用。工具列表与使用说明见第五章。

两层关系：

- Hook = 兜底的自动层，保证“总是在记、总是在召回”，用户无感。
- Skill = 精准的手动层，用于“我明确要存 / 查 / 列 / 改 / 删某条记忆”的场景。
- 两者协同，互不冲突。Hook 异步写入不会覆盖 Skill 的显式删除（防复活黑名单保障）。

### 4.3 备份与升级保障

为确保 QwenPaw 升级、备份、换机时整个记忆系统可用，设计了双重保障机制：

- **用户级插件路径**：所有代码位于 `~/.qwenpaw/plugins/`，QwenPaw 升级只覆盖安装目录，不影响插件。
- **startup/shutdown 钩子自动同步**：每次启动 / 退出时将插件目录同步到 workspace/_mem0_backup/，因此 QwenPaw 自带的“备份 workspace”功能会完整包含 Mem0 部署代码。
- **_RESTORE_INFO.json 元信息**：备份目录附带源路径和同步时间戳，恢复时可精准还原。

## 五、5 个 Skill 工具使用说明

智能体在对话中可直接使用以下 5 个工具。你可以通过自然语言让智能体调用，例如：“帮我用 add_memory 记住我负责的是化工风险预警项目” 或 “用 search_memory 查一下之前记录的部署规格”。

### add_memory — 新增记忆

- 参数：
  - `content: str` — 要记住的文本内容（一条事实 / 偏好 / 结论）
  - `user_id: str`（可选，默认 = 当前智能体名）
  - `metadata: Dict`（可选，附加标签等）
- 示例指令：
  1. 明确要记住：“帮我记住：私有化部署用 4 张 RTX 4090、INT8 精度”
  2. 手动触发：“调用 add_memory 写入：每周五下午出风险周报”
- 适用场景：存储重要事实、偏好、结论、承诺，避免 Hook 自动抽取遗漏。
- 使用提示：单条 content 建议 ≤ 2000 字符，且只表达同一主题，不要把多条不相关信息塞进一条。

### search_memory — 检索记忆

- 参数：
  - `query: str` — 检索关键词或问题
  - `user_id: str`（可选，默认 = 当前智能体名）
  - `limit: int`（可选，默认 5，最大 20）
- 示例指令：
  1. 直接问：“我之前记录的部署方案是什么？”
  2. 显式调工具：“用 search_memory 查询‘向量库’相关记忆，返回 10 条”
- 适用场景：上下文丢失时补全、跨会话回溯历史、确认之前说过的内容。
- 使用提示：query 尽量具体，避免单个字；limit 越大召回越全但噪音也可能增加。

### list_all_memories — 列出全部记忆

- 参数：
  - `user_id: str`（可选，默认 = 当前智能体名）
  - `limit: int`（可选，默认 100）
- 示例指令：
  1. “你都记住了我哪些信息？用 list_all_memories 列出来”
  2. “调用 list_all_memories 查看当前智能体的全部记忆”
- 适用场景：总览当前智能体记住的所有内容、批量核对、清理前盘点。
- 使用提示：该工具按 user_id 列出记忆列表，不支持按单个 ID 查询；需要某条详情时，先 list / search 拿到 id，再用 update / delete 操作。

### update_memory — 更新已有记忆

- 参数：
  - `memory_id: str` — 要更新的记忆 ID
  - `new_content: str` — 新的文本内容
  - `old_content: str`（可选，旧内容；传入后会加入防复活黑名单，避免旧内容被异步回写覆盖）
  - `user_id: str`（可选，默认 = 当前智能体名）
- 示例指令：
  1. “把 id=m12345 的记忆改成：向量库改用私有化 Milvus”
  2. 纠正 Hook 自动抽取错误的记忆内容
- 适用场景：修正错误抽取、更新过期信息、补充内容。
- 使用提示：建议先用 search_memory / list_all_memories 定位到目标 id 与旧内容，再更新；尽量带上 old_content 以启用防复活保护。

### delete_memory — 删除记忆

- 参数：
  - `memory_id: str` — 要删除的记忆 ID
  - `memory_content: str`（可选，记忆原文；**强烈建议传入**，会把内容及其变体一并加入防复活黑名单）
  - `user_id: str`（可选，默认 = 当前智能体名）
- 示例指令：
  1. “删除 id=m12345 的那条错误记忆”
  2. “清除我所有关于‘临时测试’的记忆（先 search 再逐条 delete）”
- 适用场景：清除错误记忆、过期信息、脱敏清理。
- 使用提示：删除后会写入防复活黑名单（默认 TTL 900s），避免异步回写让记忆“复活”。

## 六、常见问题与排查

### Q1. 启动后没有看到 mem0-integration 日志？

排查步骤：

1. 确认插件目录存在：`~/.qwenpaw/plugins/mem0-integration/`
2. 检查 plugin.json 是否存在且为合法 JSON
3. 确认使用的是原生启动路径（QwenPaw Desktop.vbs）而非第三方启动器
4. 查看 QwenPaw 主日志中是否有 “Failed to load plugin mem0-integration” 报错
5. 运行 `python test_plugin.py` 单独验证注入逻辑（脚本默认按 Path.home 定位，也可用环境变量 `MEM0_PLUGIN_BACKEND` 指定 backend.py 路径）

### Q2. 输入“我喜欢吃蔬菜”后，后台没有看到数据？

排查步骤：

1. 数据写入是异步的，等待 3–5 秒再刷新后台
2. 登录火山引擎控制台 → Mem0 → 选择对应 Project → 查看 Memory 列表
3. 确认 user_id 筛选条件：应等于智能体的 name
4. 运行 `python mem0_service/test_connection.py`，检查网络与 api_key
5. 检查 MemoryCoordinator 队列是否报错（搜索日志中的 “queue full”）

### Q3. 智能体回答“我喜欢吃什么”时回答错误？

排查步骤：

1. 确认对话是在同一智能体下（不同智能体记忆隔离）
2. 确认是“新建对话”后提问（同一上下文中 LLM 自身就记得，无需记忆层）
3. 检查 pre_reply 钩子是否触发（日志中应有 “search() returned N memories”）
4. 在火山引擎后台检索该 user_id 下是否有对应记忆
5. 若记忆存在但未召回，手动调用：“用 search_memory 查询 喜欢吃什么”

### Q4. 备份后换电脑，如何恢复 Mem0 插件？

恢复步骤：

1. 在新电脑安装并启动一次 QwenPaw，生成 ~/.qwenpaw/ 目录
2. 关闭 QwenPaw
3. 从备份中提取 _mem0_backup/ 目录内容
4. 将所有文件复制到 `~/.qwenpaw/plugins/mem0-integration/`
5. 参考 _RESTORE_INFO.json 校验源路径结构，并重新创建 `mem0_service/.env`（Key 不会随备份外泄，需重新填写）
6. 重新启动 QwenPaw，按 3.1 节验证插件加载

### Q5. 出现 “No module named agentscope.tool._builtin._scripts” 错误？

原因：agentscope 1.0.20 内部模块名已重构，某些旧脚本可能引用旧路径。

修复：

- 本项目使用的是 `from agentscope.tool import Toolkit`（非 _scripts），不受影响
- 确认所有 import 语句均使用最新 API
- 若第三方脚本触发此错，请将其升级为 agentscope 1.0.20 兼容写法

### Q6. API Key 泄露了怎么办？

紧急步骤：

1. 登录火山引擎控制台 → Mem0 → API Key 管理
2. 立即禁用 / 删除泄露的 Key
3. 生成新的 Key
4. 只修改本地 `mem0_service/.env` 中的 `VOLC_MEM0_API_KEY`（配置全部从环境变量读取，无需改动代码）
5. 重启 QwenPaw 生效

> 本仓库通过 `.gitignore` 排除 `.env`，真实 Key 不会进入版本库；请勿在代码中硬编码 Key。

### Q7. 记忆写入延迟大或队列满？

- 检查网络：到 mem0-cn-beijing.volces.com 的延迟应尽量 < 100ms
- 队列满（日志 “Memory queue is full, dropping write”）：暂时降低对话频率，或调大环境变量 `MEM0_QUEUE_MAX_SIZE`
- 令牌桶触顶：默认 18 QPS（火山引擎硬限 20 QPS），正常使用不会触顶；批量导入需自行放宽，但不得超过 20

## 七、后台数据查看（火山引擎控制台）

### 7.1 登录与导航

1. 打开浏览器访问 https://console.volcengine.com/ 登录火山引擎账号。
2. 顶部搜索栏输入 “Mem0”，进入 “Mem0（记忆系统）” 控制台。
3. 左侧菜单选择对应 Project（创建 API Key 时所属的项目）。
4. 在 “记忆 / Memories” 页签中即可查看所有写入的数据。

### 7.2 数据筛选方法

常用筛选条件：

- user_id = 智能体 name（例如 “默认智能体”、“助手-001”）
- 时间范围：最近 1 天 / 最近 7 天（异步写入有 1–3 秒延迟，等待后再查）
- metadata.tag：如果 Skill 调用时传入了 metadata，可按标签筛选
- 全文检索：输入 “蔬菜”、“部署” 等关键词直接命中 content

### 7.3 数据量与用量统计

在控制台 “用量 / Quota” 页面可查看：

- Memory 数量（条）
- Token 消耗（检索 / 写入）
- API 调用次数（QPS / 总次数）
- 到期时间与套餐余量

## 八、维护与建议

### 8.1 日常维护清单

| 频率 | 操作 |
|---|---|
| 每周一次 | 检查火山引擎后台用量是否接近套餐上限 |
| 每月一次 | 清理过期 / 错误记忆（先 search / list 再 delete） |
| 每季度一次 | 轮换 API Key（Q6 步骤），并同步更新 `.env` |
| QwenPaw 升级后 | 立即按 3.1 节验证插件加载与记忆读写 |
| 换机 / 重装系统后 | 按 Q4 从备份恢复，并执行完整性验证 |

### 8.2 最佳实践

- **重要信息用 add_memory 显式存**：关键结论、固定偏好、重要承诺等，不要只依赖 Hook 自动抽取。
- **智能体命名清晰**：user_id = agent.name，命名应唯一且可读（避免 “助手1”“助手2”）。
- **metadata 加标签**：调用 add / update 时传入 `{"tag": "项目/日程/偏好"}` 方便后台筛选。
- **定期手动备份**：虽然 shutdown 钩子自动同步，重大变更前仍建议手动复制一次插件目录。
- **敏感信息先脱敏再记忆**：手机号、证件号、账号口令等不建议明文写入长期记忆。

### 8.3 日志与诊断

关键日志关键词（可在 QwenPaw 日志中搜索）：

| 日志 | 含义 |
|---|---|
| `[mem0-integration] startup hook triggered` | 插件启动成功 |
| `[MemoryCoordinator] worker thread started` | 异步队列启动 |
| `pre_reply search() returned N memories` | 自动检索召回 N 条 |
| `post_reply enqueued write for user_id=X` | 异步写入入队 |
| `Memory queue is full, dropping write` | 队列已满，写入丢弃 |
| `blacklisted ... skipping revive` | 防复活生效（正常现象） |
