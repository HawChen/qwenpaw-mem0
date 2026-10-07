"""Mem0 记忆后端 —— 实现 QwenPaw 2.x 官方的 ``BaseMemoryManager`` 接口。

为什么这样改
------------
旧方案通过 ``QwenPawAgent.register_class_hook`` 注入 AgentScope 类钩子，
但 agentscope 2.0.7 已移除 ``register_class_hook`` / ``agentscope.memory`` /
``_MemoryMark``，整条路径失效。QwenPaw 2.x 改为提供官方记忆后端接口，
语义与旧方案一一对应：

====================  ==========================  ==========================
旧方案（已失效）        新方案（本文件）              说明
====================  ==========================  ==========================
``pre_reply`` 钩子      ``auto_memory_search()``    回复前自动检索并注入
``post_reply`` 钩子     ``auto_memory()``           批量抽取写入（框架自带队列）
5 个 Skill 工具         ``list_memory_tools()``     显式工具
自建异步队列            框架 ``submit_auto_memory``  无需自己实现
====================  ==========================  ==========================

接口契约（对照官方 ``plugins/memory/adbpg`` 参考实现）
------------------------------------------------------
- 构造：``factory(context=MemoryBackendContext)``，子类**必须**调用
  ``super().__init__(context=context)`` —— 基类会建立
  ``_auto_memory_task_queue`` / ``_auto_memory_worker_task`` /
  ``_auto_memory_worker_stopping`` 等共享状态，并把实例注册到
  ``memory_registry``。缺少这一步会让 ``/new``、``/compact`` 在
  ``submit_auto_memory`` 中抛 ``AttributeError``。
- ``start()`` / ``auto_memory()`` / ``memory_search()`` /
  ``get_auto_memory_search_options()`` 均为 **async**。
- 释放资源覆盖 ``async def _close_backend()``，不要覆盖 ``close()`` ——
  基类 ``close()`` 负责先停共享 worker，再调用本扩展点。
- 自动检索的编排（合成工具调用消息、注入上下文）由基类
  ``auto_memory_search()`` 完成，本类只提供检索实现（``memory_search``）
  与开关（``get_auto_memory_search_options``），**不要覆盖**。
- ``list_memory_tools()`` 返回的每个工具必须是返回 ``ToolChunk`` 的可调用对象。

设计原则
--------
冻结运行时无法读源码，接口细节存在不确定性，因此：
- 所有外部调用（框架类、上下文对象、返回结构）**全部带兜底**
- 所有失败路径**只记日志、不抛异常**，绝不阻塞工作区启动
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .content_filter import DedupCache, is_noise, normalize_text, should_write, strip_tool_messages
from .http_client import Mem0HttpClient
from .tools import MemoryTools
from .usage_meter import UsageMeter

logger = logging.getLogger("qwenpaw_mem0")
BACKEND_ID = "mem0"


def _log(msg: str) -> None:
    """同时写标准日志与插件文件日志（便于诊断）。"""
    logger.info(msg)
    try:
        import datetime

        from pathlib import Path

        path = Path(__file__).resolve().parent / "backend.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 框架契约导入（公共 API 优先，内部路径兜底）
# ---------------------------------------------------------------------------
try:
    from qwenpaw.memory import (  # type: ignore
        NO_RELEVANT_MEMORIES,
        AutoMemorySearchOptions,
        BaseMemoryManager,
        MemoryBackendContext,
    )
except Exception as exc:  # noqa: BLE001
    _log(f"[backend] ⚠️ qwenpaw.memory 导入失败，改用内部路径: {exc}")
    try:
        from qwenpaw.agents.memory.base_memory_manager import (  # type: ignore
            NO_RELEVANT_MEMORIES,
            AutoMemorySearchOptions,
            BaseMemoryManager,
            MemoryBackendContext,
        )
    except Exception as exc2:  # noqa: BLE001
        _log(f"[backend] ⚠️ 内部路径导入失败: {exc2}")
        NO_RELEVANT_MEMORIES = "No relevant memories found."
        AutoMemorySearchOptions = None  # type: ignore[assignment]
        MemoryBackendContext = None  # type: ignore[assignment]
        BaseMemoryManager = object  # type: ignore[assignment,misc]

try:
    from agentscope.message import TextBlock, ToolResultState  # type: ignore
    from agentscope.tool import ToolChunk  # type: ignore
except Exception as exc:  # noqa: BLE001
    _log(f"[backend] ⚠️ agentscope 导入失败（工具将降级为纯文本）: {exc}")
    TextBlock = None  # type: ignore[assignment]
    ToolResultState = None  # type: ignore[assignment]
    ToolChunk = None  # type: ignore[assignment]


def _tool_chunk(text: str, ok: bool = True) -> Any:
    """构造标准 ToolChunk（与官方 adbpg / ReMe 后端一致）。"""
    if ToolChunk is None or TextBlock is None:  # 极端降级
        return text
    state = ToolResultState.SUCCESS if ok else ToolResultState.ERROR
    return ToolChunk(
        is_last=True,
        state=state,
        content=[TextBlock(type="text", text=text)],
    )


class Mem0MemoryManager(BaseMemoryManager):  # type: ignore[misc,valid-type]
    """火山引擎 Mem0 记忆后端。

    由 ``api.register_memory_backend(backend_id="mem0", factory=Mem0MemoryManager)``
    注册，用户在 ``agent.json`` 中把 ``memory_manager_backend`` 设为 ``"mem0"``
    即可对指定 Agent 启用（其余 Agent 继续使用本地 ReMe）。
    """

    def __init__(self, context: Any = None, **kwargs: Any) -> None:
        # 框架以 factory(context=MemoryBackendContext) 关键字调用；
        # 兼容位置参数与旧字段名 backend_context。
        if context is None:
            context = kwargs.get("context") or kwargs.get("backend_context")
        if context is None:
            raise TypeError("Mem0MemoryManager 需要 MemoryBackendContext 参数")

        # ------------------------------------------------------------------
        # 关键：初始化基类 —— 建立 worker 队列 / _auto_memory_worker_stopping
        # 等共享状态，并把实例注册到 memory_registry。
        # ------------------------------------------------------------------
        super().__init__(context=context)

        self._context = context
        self._backend_config = getattr(context, "backend_config", None) or {}

        # token 估算系数（仅用于自动检索合成消息的上下文统计）
        raw_divisor = getattr(context, "token_estimate_divisor", None)
        try:
            self._token_divisor = (
                float(raw_divisor) if raw_divisor and float(raw_divisor) > 0 else 4.0
            )
        except (TypeError, ValueError):
            self._token_divisor = 4.0

        self._client: Optional[Mem0HttpClient] = None
        self._tools: Optional[MemoryTools] = None
        self._dedup = DedupCache(ttl=CONFIG.blacklist_ttl)

        self._meter = UsageMeter(
            CONFIG.usage_file,
            credit_per_token=CONFIG.credit_per_token,
            yuan_per_million_credit=CONFIG.yuan_per_million_credit,
            monthly_budget_yuan=CONFIG.monthly_budget_yuan,
            enabled=CONFIG.usage_enabled,
        )
        self._meter.set_token_divisor(raw_divisor)

        self._interval = self._cfg_int("auto_memory_interval", CONFIG.auto_memory_interval)
        self._max_results = self._cfg_int("max_results", CONFIG.max_results)
        self._min_write_length = self._cfg_int("min_write_length", CONFIG.min_write_length)
        self._search_enabled = bool(
            self._cfg("memory_search_enabled", CONFIG.memory_search_enabled)
        )
        _log(
            f"[backend] Mem0MemoryManager 已创建: agent_id={self.agent_id}, "
            f"working_dir={self.working_dir}, "
            f"interval={self._interval}, max_results={self._max_results}"
        )

    # ------------------------------------------------------------------
    # 配置读取（框架的 backend_config 优先于 .env）
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._backend_config.get(key)  # type: ignore[union-attr]
            return default if value is None else value
        except Exception:  # noqa: BLE001
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """初始化客户端并校验凭据（失败仅记日志，不阻塞工作区）。"""
        errors = CONFIG.validate()
        if errors:
            for e in errors:
                _log(f"[backend] 配置错误: {e}")
            return

        def _build_and_ping() -> tuple:
            client = Mem0HttpClient(
                api_key=CONFIG.volc_mem0_api_key,
                host=CONFIG.volc_mem0_host,
                connect_timeout=CONFIG.connect_timeout,
                read_timeout=CONFIG.read_timeout,
                qps_limit=CONFIG.qps_limit,
            )
            return client, client.ping()

        try:
            client, info = await asyncio.to_thread(_build_and_ping)
            self._client = client
            self._tools = MemoryTools(
                client=client,
                user_id=self.agent_id,
                meter=self._meter,
                dedup=self._dedup,
            )
            _log(f"[backend] Mem0 连接成功: agent_id={self.agent_id}, ping_ok={bool(info)}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[backend] ⚠️ Mem0 初始化失败: {type(exc).__name__}: {exc}")
            self._client = None
            self._tools = None

    async def _close_backend(self) -> bool:
        """释放后端资源（基类 ``close()`` 已先停共享 worker）。"""
        client = self._client
        self._client = None
        self._tools = None
        if client is None:
            return True
        try:
            await asyncio.to_thread(client.close)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("[Mem0] 关闭客户端失败")
            return False

    # ------------------------------------------------------------------
    # 自动记忆：写入（框架 worker 串行调用本方法）
    # ------------------------------------------------------------------
    async def auto_memory(self, messages: List[Any], **kwargs: Any) -> str:
        """抽取并持久化一批消息（对应旧方案的 post_reply 写入）。

        ``get_auto_memory_interval()`` 决定攒多少轮用户回复后触发一次，
        以摊薄服务端抽取的固定开销（降本设计）。
        """
        if self._client is None:
            return ""

        # 0) 移除框架自动检索注入的合成消息（避免把"检索动作"写进记忆）
        try:
            messages = self._messages_without_auto_memory_search(messages)
        except Exception:  # noqa: BLE001
            pass

        # 1) 归一化为 {"role", "content"}
        normalized: List[Dict[str, str]] = []
        for m in messages or []:
            role = str(
                getattr(m, "role", "")
                or (m.get("role") if isinstance(m, dict) else "")
                or "user"
            )
            text = normalize_text(m)
            if text:
                normalized.append({"role": role, "content": text})
        if not normalized:
            return "无有效内容，已跳过写入。"

        # 2) 降本：丢弃工具输出
        filtered = strip_tool_messages(normalized, keep_tool=CONFIG.keep_tool_messages)
        if not filtered:
            return "仅含工具输出，已按降本策略跳过写入。"

        # 3) 噪音 / 长度过滤
        if not should_write(filtered, self._min_write_length):
            return "内容过短或为噪音，已跳过写入。"

        # 4) 内容去重
        joined = " ".join(m["content"] for m in filtered)
        if self._dedup.is_duplicate(joined):
            return "内容重复，已跳过写入。"

        # 5) 费用熔断
        if not self._meter.is_write_allowed():
            return "已达月度预算上限，已暂停写入（检索不受影响）。"

        # 6) 写入
        try:
            await asyncio.to_thread(
                self._client.add,
                messages=filtered,
                user_id=self.agent_id,
                metadata={"source": "auto_memory"},
            )
            self._meter.record("add_auto", len(joined))
            return f"已写入 {len(filtered)} 条消息（服务端异步落库，约 1-3 分钟可见）。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] auto_memory 写入失败: %s", exc)
            return f"写入失败：{exc}"

    # ------------------------------------------------------------------
    # 自动记忆：检索开关与实现
    # ------------------------------------------------------------------
    async def get_auto_memory_search_options(self) -> Any:
        """自动检索开关与参数（返回 None 表示禁用自动检索）。

        框架 ``auto_memory_search()`` 会据此调用 ``memory_search()``，
        并把结果包装成合成的工具调用消息注入模型上下文。
        """
        if self._client is None or not self._search_enabled:
            return None
        if AutoMemorySearchOptions is None:
            return None
        return AutoMemorySearchOptions(
            max_results=max(1, int(self._max_results)),
            estimate_divisor=self._token_divisor,
        )

    async def memory_search(self, query: str, max_results: int = 5, **kwargs: Any) -> Any:
        """检索长期记忆（自动检索与 ``memory_search`` 工具共用）。"""
        query = (query or "").strip()
        if not query:
            return _tool_chunk("检索失败：query 不能为空。", ok=False)
        if self._client is None:
            return _tool_chunk("Mem0 未就绪，无法检索。", ok=False)
        try:
            top_k = max(1, min(int(max_results or 5), 20))
        except (TypeError, ValueError):
            top_k = 5
        try:
            results = await asyncio.to_thread(
                self._client.search,
                query=query,
                user_id=self.agent_id,
                top_k=top_k,
            )
            self._meter.record("search_auto", len(query))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] memory_search 失败: %s", exc)
            return _tool_chunk(f"检索失败：{exc}", ok=False)

        lines = []
        for r in results or []:
            text = r.get("memory") or r.get("text") or ""
            if text:
                lines.append(f"• {text}")
        if not lines:
            # 使用框架约定文本，基类据此判断"无结果"并跳过注入
            return _tool_chunk(NO_RELEVANT_MEMORIES)
        return _tool_chunk("[用户长期记忆]\n" + "\n".join(lines))

    # ------------------------------------------------------------------
    # 工具（Skill 增强层：5 个记忆管理工具）
    # ------------------------------------------------------------------
    def list_memory_tools(self) -> List[Any]:
        """返回 5 个记忆工具（异步、返回 ToolChunk，符合工具注册契约）。"""
        if self._tools is None:
            # 未就绪时降级为与基类一致的默认行为
            return [self.memory_search] if self.is_memory_search_enabled() else []
        return _make_tool_functions(self._tools)

    def is_memory_search_enabled(self) -> bool:
        return bool(self._search_enabled and self._client is not None)

    def get_memory_prompt(self) -> str:
        """注入系统提示的记忆使用引导。"""
        return (
            "你具备长期记忆能力（火山引擎 Mem0）：\n"
            "- 对话中会自动检索并注入与当前话题相关的历史记忆。\n"
            "- 需要主动回忆时，调用 search_memory 工具。\n"
            "- 需要确保持久记住某条信息时，调用 add_memory 工具显式写入。\n"
            "- 需要盘点或清理记忆时，使用 list_memories / update_memory / delete_memory。"
        )

    # ------------------------------------------------------------------
    # 框架元信息
    # ------------------------------------------------------------------
    def get_auto_memory_interval(self) -> int:
        """自动写入节奏（0 表示禁用）。

        默认 5 —— 框架按间隔批量写入，摊薄每次 ``add`` 的固定抽取提示词开销，
        这是相较旧方案"每轮都写"的天然降本点。
        """
        return max(self._interval, 0)

    def get_runtime_status(self, **kwargs: Any) -> Dict[str, Any]:
        """供状态 UI 呈现的运行快照（基类任务 + 用量记账合并）。"""
        try:
            status = super().get_runtime_status(**kwargs)
        except Exception:  # noqa: BLE001
            status = {}
        status["backend_id"] = BACKEND_ID
        status["connected"] = self._client is not None
        status["max_results"] = self._max_results
        status["search_enabled"] = self.is_memory_search_enabled()
        try:
            status["usage"] = self._meter.snapshot()
        except Exception:  # noqa: BLE001
            pass
        return status


# ---------------------------------------------------------------------------
# 工具函数工厂
# ---------------------------------------------------------------------------
def _make_tool_functions(tools: MemoryTools) -> List[Any]:
    """把同步的 ``MemoryTools`` 包装成框架要求的异步 ToolChunk 工具。

    每个包装函数：
    - ``async``（框架工具注册契约）
    - 签名只含 ``str`` / ``int`` 参数（避免 JSON Schema 生成失败）
    - 同步 REST 调用走 ``asyncio.to_thread``，不阻塞事件循环
    """

    async def search_memory(query: str, limit: int = 5) -> Any:
        """检索长期记忆。

        当需要回忆之前的对话内容、用户偏好、决定、日期、人物或待办时使用。

        Args:
            query: 检索关键词或问题，尽量具体。
            limit: 返回条数上限，默认 5。

        Returns:
            匹配到的记忆列表（含 id 与相似度）。
        """
        text = await asyncio.to_thread(tools.search_memory, query, limit)
        return _tool_chunk(text)

    async def add_memory(content: str, tags: str = "") -> Any:
        """显式写入一条长期记忆。

        用于明确要长期记住的事实、偏好、承诺或日程，避免自动抽取遗漏。

        Args:
            content: 要记住的内容，用完整的一句话表述。
            tags: 可选标签，多个用逗号分隔，例如 "饮食,偏好"。

        Returns:
            写入结果说明。
        """
        text = await asyncio.to_thread(tools.add_memory, content, tags)
        return _tool_chunk(text)

    async def list_memories(limit: int = 20) -> Any:
        """列出当前 Agent 已保存的记忆。

        用于盘点记忆内容、找出需要清理或修正的条目（拿到 id 后可配合
        update_memory / delete_memory 使用）。

        Args:
            limit: 返回条数上限，默认 20。

        Returns:
            记忆条目列表（含 id）。
        """
        text = await asyncio.to_thread(tools.list_memories, limit)
        return _tool_chunk(text)

    async def update_memory(memory_id: str, content: str) -> Any:
        """修改一条已有记忆的内容。

        用于修正错误记录、补全信息或更新已过期的内容。
        建议先用 search_memory / list_memories 拿到 memory_id。

        Args:
            memory_id: 要修改的记忆 id。
            content: 新的完整内容。

        Returns:
            更新结果说明。
        """
        text = await asyncio.to_thread(tools.update_memory, memory_id, content)
        return _tool_chunk(text)

    async def delete_memory(memory_id: str) -> Any:
        """删除一条记忆。

        用于清除错误、过期或不应保留的记忆。
        建议先用 search_memory / list_memories 拿到 memory_id。

        Args:
            memory_id: 要删除的记忆 id。

        Returns:
            删除结果说明。
        """
        text = await asyncio.to_thread(tools.delete_memory, memory_id)
        return _tool_chunk(text)

    return [search_memory, add_memory, list_memories, update_memory, delete_memory]


# 供 register_memory_backend 使用的工厂别名
def create_backend(context: Any = None, **kwargs: Any) -> "Mem0MemoryManager":
    """工厂函数（框架若按函数而非类调用 factory 时的兜底）。"""
    return Mem0MemoryManager(context, **kwargs)