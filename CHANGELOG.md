# 变更日志（Changelog）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 风格，版本号遵循语义化版本。

## [2.0.0] - 2026-10

### 背景：一次被迫的彻底重构

agentscope **2.0.7** 移除了 `register_class_hook`、`agentscope.memory` 与 `_MemoryMark`，v1 依赖类级生命周期 Hook 的整条注入路径随之失效。QwenPaw 2.x 改为提供官方记忆后端抽象 `BaseMemoryManager` 与注册接口 `register_memory_backend`。v2 据此重构，**接入方式、依赖与文件结构均为破坏性变更（Breaking Changes）**，但对用户呈现的「自动召回 + 显式工具」能力保持一致。

### Added（新增）

- `mem0_service/memory_backend.py`：`Mem0MemoryManager`，实现官方 `BaseMemoryManager`（`start` / `auto_memory` / `memory_search` / `list_memory_tools` / `get_auto_memory_search_options` / `get_auto_memory_interval` / `get_runtime_status` / `_close_backend`）。
- `mem0_service/http_client.py`：**零 mem0ai** 的 REST 客户端，仅用 `httpx` 直连火山引擎 Mem0；含令牌桶限流（默认 18 QPS）、429 指数退避重试、连接 / 读取超时。
- `mem0_service/content_filter.py`：写入降本四道闸门——批量节奏、丢弃 tool 角色消息、噪音 / 长度过滤、`DedupCache` 去重与删除防复活。
- `mem0_service/usage_meter.py`：按月分桶的用量记账与月度预算熔断（超额只停写、不停检索），应对 2026-11-02 起的正式计费。
- `mem0_service/activation.py`：加载时序激活补救，`reserve_selection → reload_agent → isinstance 实例复核`，后台线程、幂等、带重试，解决 Agent 工作区先于插件启动导致的首次回退。
- `set_mem0_agent.ps1`：按 Agent 在 `mem0`（云端计费）与 `remelight`（本地免费 ReMe）之间切换记忆后端，写入前备份、写入后校验 JSON。
- 自动写入改用框架 `submit_auto_memory` 共享队列与 `auto_memory()` 批量回调，默认每 5 轮写一次。
- `CHANGELOG.md`（本文件）。

### Changed（变更）

- 接入方式：类钩子猴子补丁 → 官方 `api.register_memory_backend(backend_id="mem0", factory=Mem0MemoryManager)`，由 Agent 的 `agent.json` 中 `running.memory_manager_backend` 选择后端。
- 显式工具由 Skill 改为后端 `list_memory_tools()` 注册，统一为异步、返回 `ToolChunk`；原 `get` / `list_all_memories` 语义合并为 `list_memories`。
- `.env` 解析改为自实现（仅标准库），不再依赖 `python-dotenv`。
- 配置项更新（见 `.env.example`）：新增 `MEM0_USAGE_ENABLED`、`MEM0_MONTHLY_BUDGET_YUAN`、`MEM0_CREDIT_PER_TOKEN`、`MEM0_YUAN_PER_MILLION_CREDIT`、`MEM0_MEMORY_SEARCH_ENABLED`、`MEM0_AUTO_MEMORY_INTERVAL`、`MEM0_KEEP_TOOL_MESSAGES`、`MEM0_MIN_WRITE_LENGTH`；自动召回默认条数由 5 调整为 3。
- 运行时依赖收敛为**仅 `httpx`**。
- `plugin.json` 版本升至 `2.0.0`。

### Removed（移除）

- `mem0_service/hooks/`（4 个类级生命周期 Hook）、`mem0_service/skills/`（旧 Skill 工具）、`mem0_service/utils/coordinator.py`（自建异步队列 / 单例 / 黑名单协调器）。
- `mem0_service/qwenpaw_client/`（基于 `mem0ai` SDK 的封装）、`mem0_service/api/`（可选 FastAPI 独立服务）。
- `test_plugin.py`、`mem0_service/test_connection.py`（旧注入路径自测，已不适用；连通性改由后端 `start()` 阶段的 `ping` 校验与说明书的手动验证覆盖）。
- 依赖：`mem0ai`、`fastapi`、`uvicorn`、`pydantic`、`python-dotenv`。
- 配置项：`MEM0_SERVICE_HOST` / `MEM0_SERVICE_PORT`（FastAPI 服务）、`MEM0_QUEUE_MAX_SIZE`（自建队列）。

### 从 v1 迁移

1. **备份**：保留旧插件目录与 `mem0_service/.env`（其中的 `VOLC_MEM0_HOST` / `VOLC_MEM0_API_KEY` 可继续使用）。
2. **替换文件**：用 v2 的 `backend.py`、`plugin.json`、`set_mem0_agent.ps1` 与整个 `mem0_service/` 覆盖旧版本；删除上文 Removed 列出的目录 / 文件。
3. **更新依赖**：v2 仅需 `httpx`（QwenPaw 内置环境已自带），可卸载 / 忽略 `mem0ai`、`fastapi` 等旧依赖。
4. **更新 `.env`**：对照新的 `mem0_service/.env.example` 补齐预算 / 降本配置；删除 `MEM0_SERVICE_*`、`MEM0_QUEUE_MAX_SIZE` 等旧键。
5. **选择后端**：v2 不再自动注入，需显式为目标 Agent 选择后端——运行 `set_mem0_agent.ps1 -AgentId <id>`，或手动把该 Agent `agent.json` 的 `running.memory_manager_backend` 设为 `"mem0"`。
6. **重启 QwenPaw**：插件注册后约 20 秒自动激活；日志出现 `✓ 激活成功，真实实例: [...]` 即完成。

## [1.0.0]

- 初版：基于 AgentScope 1.x 的 `register_class_hook` 实现「回复前自动召回、回复后异步写入」的 4 个类级 Hook，配合 5 个 Skill 工具构成双层记忆。
- 自建异步队列 + 令牌桶限流 + 线程安全单例 + 删除防复活黑名单，提供可选 FastAPI 独立服务，依赖 `mem0ai` SDK。
- 适用于 QwenPaw / AgentScope 1.x；**agentscope 2.0.7 起已不可用，请升级到 2.0.0**。
