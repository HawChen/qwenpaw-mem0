"""qwenpaw_mem0_service — 火山引擎 Mem0 记忆系统 (Hook 兜底 + Skill 增强).

本包为 QwenPaw (基于 AgentScope) 提供长期记忆能力，分为两层：

- **Hook 兜底层** (``hooks``)：通过 ``register_class_hook`` 注册到
  ``QwenPawAgent`` 类级别，在 ``pre_reply`` 时自动检索记忆并注入上下文，
  在 ``post_reply`` 时异步写入对话内容。对所有智能体实例自动生效。

- **Skill 增强层** (``skills``)：提供 5 个显式记忆管理工具
  (search / list / delete / update / add)，供 LLM 按需调用。

核心协调器 ``MemoryCoordinator`` 封装了线程安全单例、令牌桶限流、
异步写入队列和防复活黑名单，确保火山引擎 API 调用不会卡死主进程。
"""
