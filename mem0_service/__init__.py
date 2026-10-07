"""qwenpaw_mem0_service — 火山引擎 Mem0 记忆后端（QwenPaw 2.x 版）。

本包为 QwenPaw 2.x 提供长期记忆能力，采用官方记忆后端接口：

- **自动记忆层**（``memory_backend.Mem0MemoryManager``）：
  实现 ``BaseMemoryManager``，由框架在回复前调用 ``auto_memory_search()``
  自动检索并注入，按间隔调用 ``auto_memory()`` 批量抽取写入。
  对应旧方案的 "Hook 兜底"。

- **显式工具层**（``tools.MemoryTools``）：
  通过 ``list_memory_tools()`` 暴露 5 个记忆管理工具
  （search / add / list / update / delete），供 LLM 按需调用。
  对应旧方案的 "Skill 增强"。

支撑模块：

- ``http_client``：零依赖 Mem0 REST 客户端（仅用内置 httpx），
  替代不可用的 ``mem0ai``。含令牌桶限流与限流退避重试。
- ``content_filter``：写入前置过滤与去重（降本核心，工具输出不入记忆）。
- ``usage_meter``：用量记账与月度预算熔断（应对 2026-11-02 起正式计费）。
- ``activation``：后端激活补救 —— QwenPaw 2.x 的 Agent 工作区先于插件加载启动，
  导致注册"晚了一步"而回退到 remelight；本模块在注册完成后对目标 Agent
  调用官方零停机重载接口，触发记忆后端重新解析。
- ``config``：配置加载（自实现 .env 解析，只依赖标准库）。
"""

__all__ = [
    "activation",
    "config",
    "content_filter",
    "http_client",
    "memory_backend",
    "tools",
    "usage_meter",
]