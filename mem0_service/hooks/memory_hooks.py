"""Hook 兜底层 — 四钩子全注册 + 去重逻辑。

通过 ``register_class_hook`` 注册到 ``QwenPawAgent`` 类级别，
对所有智能体实例自动生效，无需 Agent 显式调用。

四钩子分工 (覆盖手册 pre_reasoning + post_acting 和实际 pre_reply + post_reply):

1. ``pre_reply`` (主检索): 在 reply() 前检索记忆, 注入 Hint, 设置去重标志
2. ``pre_reasoning`` (兜底检索): 在 _reasoning() 前检查标志, 若未检索则兜底
3. ``post_acting`` (工具收集): 在 _acting() 后收集工具调用结果到缓冲区
4. ``post_reply`` (主写入): 在 reply() 后合并写入, 重置所有标志

去重逻辑:
- 检索去重: ``_mem0_searched`` 标志, pre_reply 设置后 pre_reasoning 跳过
- 写入去重: ``_mem0_tool_collected`` 标志, post_acting 收集后 post_reply 合并写入
- 标志重置: post_reply 结束时重置所有标志, 准备下一轮对话
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("qwenpaw_mem0")

# 文件日志路径 (固定绝对路径，不被 QwenPaw logging 重配置影响)
_HOOK_LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "injection.log",
)


def _hook_log(msg: str) -> None:
    """写文件日志，用于诊断 Hook 是否被触发。"""
    try:
        with open(_HOOK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} [hook] {msg}\n")
    except Exception:
        pass


class MemoryHooks:
    """Hook 兜底层：四钩子全注册 + 去重逻辑。

    所有方法均为 async，签名兼容 AgentScope ``register_class_hook``
    的 ``pre_reply`` / ``post_reply`` / ``pre_reasoning`` / ``post_acting``
    钩子类型。

    Args:
        max_chars: 注入记忆文本的最大字符数 (截断保护，防止 Token 超限)。
        min_write_length: 异步写入的最小消息长度 (过滤噪音)。
    """

    def __init__(
        self,
        max_chars: int = 800,
        min_write_length: int = 10,
    ) -> None:
        self.max_chars = max_chars
        self.min_write_length = min_write_length

    # ================================================================
    # 检索层: pre_reply (主) + pre_reasoning (兜底)
    # ================================================================

    async def pre_reply(
        self,
        agent: Any,
        kwargs: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """主检索：在 reply() 前检索记忆并注入 Hint。

        设置 ``agent._mem0_searched = True`` 作为去重标志，
        ``pre_reasoning`` 会检查此标志避免重复检索。

        Args:
            agent: QwenPawAgent 实例。
            kwargs: reply 方法的输入参数 (包含 ``msg``)。

        Returns:
            None (不修改 kwargs，通过 agent.memory 注入)。
        """
        try:
            agent_name = getattr(agent, "name", "?")
            _hook_log(f"pre_reply 触发: agent={agent_name}")

            # 重置本轮所有标志 (新一轮 reply 开始)
            self._reset_flags(agent)

            msg = kwargs.get("msg")
            if msg is None or (isinstance(msg, list) and not msg):
                _hook_log(f"pre_reply 跳过: msg 为空, agent={agent_name}")
                return None

            # 提取用户最新消息文本
            last_msg = msg[-1] if isinstance(msg, list) else msg
            query = self._extract_text(last_msg)
            if not query or len(query.strip()) < 2:
                _hook_log(f"pre_reply 跳过: query 过短='{query}', agent={agent_name}")
                return None

            # 跳过命令消息
            command_handler = getattr(agent, "command_handler", None)
            if command_handler is not None:
                try:
                    if command_handler.is_command(query):
                        _hook_log(f"pre_reply 跳过: 命令消息, agent={agent_name}")
                        return None
                except Exception:
                    pass

            # 执行检索
            self._do_search(agent, query, agent_name)

            # 设置去重标志
            agent._mem0_searched = True
            _hook_log(f"pre_reply 完成: _mem0_searched=True, agent={agent_name}")

        except Exception as exc:
            logger.warning("[Mem0] pre_reply hook 失败: %s", exc)
        return None

    async def pre_reasoning(
        self,
        agent: Any,
        kwargs: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """兜底检索：在 _reasoning() 前检查是否已检索。

        若 ``pre_reply`` 已设置 ``_mem0_searched`` 则跳过 (去重)；
        否则作为兜底执行检索 (覆盖 pre_reply 失败的场景)。

        Args:
            agent: QwenPawAgent 实例。
            kwargs: _reasoning 方法的输入参数。

        Returns:
            None (不修改 kwargs)。
        """
        try:
            agent_name = getattr(agent, "name", "?")
            _hook_log(f"pre_reasoning 触发: agent={agent_name}")

            # 去重: pre_reply 已检索则跳过
            if getattr(agent, "_mem0_searched", False):
                _hook_log(f"pre_reasoning 跳过: 已检索 (去重), agent={agent_name}")
                return None

            # 兜底检索: 从 kwargs 提取查询
            msg = kwargs.get("msg") or kwargs.get("messages")
            if msg is None:
                _hook_log(f"pre_reasoning 跳过: 无 msg, agent={agent_name}")
                return None

            # 提取查询文本
            if isinstance(msg, list):
                query = self._extract_text(msg[-1]) if msg else ""
            else:
                query = self._extract_text(msg)

            if not query or len(query.strip()) < 2:
                _hook_log(f"pre_reasoning 跳过: query 过短, agent={agent_name}")
                return None

            # 执行检索
            self._do_search(agent, query, agent_name)
            agent._mem0_searched = True
            _hook_log(f"pre_reasoning 兜底检索完成: _mem0_searched=True, agent={agent_name}")

        except Exception as exc:
            logger.warning("[Mem0] pre_reasoning hook 失败: %s", exc)
        return None

    # ================================================================
    # 写入层: post_acting (工具收集) + post_reply (主写入)
    # ================================================================

    async def post_acting(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        output: Any,
    ) -> Any:
        """工具调用后：收集工具调用结果到缓冲区。

        不直接写入 Mem0，而是将工具调用结果添加到
        ``agent._mem0_tool_buffer``，由 ``post_reply`` 统一合并写入。

        Args:
            agent: QwenPawAgent 实例。
            kwargs: _acting 方法的输入参数。
            output: 工具调用结果。

        Returns:
            None (不修改输出)。
        """
        try:
            agent_name = getattr(agent, "name", "?")
            _hook_log(f"post_acting 触发: agent={agent_name}")

            # 心跳过滤
            if self._is_heartbeat(kwargs, output):
                _hook_log(f"post_acting 跳过: 心跳, agent={agent_name}")
                return None

            # 提取工具调用结果
            tool_result = self._extract_text(output)
            if not tool_result or len(tool_result.strip()) < 2:
                _hook_log(f"post_acting 跳过: 结果为空, agent={agent_name}")
                return None

            # 收集到缓冲区
            if not hasattr(agent, "_mem0_tool_buffer"):
                agent._mem0_tool_buffer = []
            agent._mem0_tool_buffer.append(
                {"role": "tool", "content": tool_result},
            )
            agent._mem0_tool_collected = True
            _hook_log(
                f"post_acting 收集: buffer={len(agent._mem0_tool_buffer)} 条, agent={agent_name}",
            )

        except Exception as exc:
            logger.warning("[Mem0] post_acting hook 失败: %s", exc)
        return None

    async def post_reply(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        output: Any,
    ) -> Any:
        """主写入：在 reply() 后合并写入对话 + 工具结果。

        合并 ``kwargs.msg`` (用户消息) + ``_mem0_tool_buffer`` (工具结果)
        + ``output`` (最终回复) 为完整消息列表，异步写入 Mem0。
        写入完成后重置所有标志。

        Args:
            agent: QwenPawAgent 实例。
            kwargs: reply 方法的输入参数。
            output: Agent 的回复消息 (Msg)。

        Returns:
            None (不修改输出)。
        """
        try:
            agent_name = getattr(agent, "name", "?")
            _hook_log(f"post_reply 触发: agent={agent_name}")

            # 心跳过滤
            if self._is_heartbeat(kwargs, output):
                _hook_log(f"post_reply 跳过: 心跳, agent={agent_name}")
                self._reset_flags(agent)
                return None

            msg = kwargs.get("msg")
            if msg is None:
                _hook_log(f"post_reply 跳过: msg 为空, agent={agent_name}")
                self._reset_flags(agent)
                return None

            # 构建消息列表 (用户消息 + 工具缓冲区 + 最终回复)
            messages = []

            # 1. 用户消息
            if isinstance(msg, list):
                for m in msg:
                    text = self._extract_text(m)
                    role = getattr(m, "role", "user")
                    if text:
                        messages.append({"role": role, "content": text})
            else:
                text = self._extract_text(msg)
                if text:
                    role = getattr(msg, "role", "user")
                    messages.append({"role": role, "content": text})

            # 2. 工具调用结果 (去重: 从 post_acting 缓冲区获取)
            tool_buffer = getattr(agent, "_mem0_tool_buffer", [])
            if tool_buffer:
                messages.extend(tool_buffer)
                _hook_log(
                    f"post_reply 合并工具缓冲区: {len(tool_buffer)} 条, agent={agent_name}",
                )

            # 3. 最终回复
            if output is not None:
                reply_text = self._extract_text(output)
                if reply_text:
                    messages.append(
                        {"role": "assistant", "content": reply_text},
                    )

            # 过滤过短消息
            total_len = sum(len(m["content"]) for m in messages)
            if total_len < self.min_write_length:
                _hook_log(f"post_reply 跳过: 消息过短 ({total_len} chars), agent={agent_name}")
                self._reset_flags(agent)
                return None

            user_id = getattr(agent, "name", "default_user")
            _hook_log(f"post_reply 入队: {len(messages)} 条消息, user_id={user_id}")

            from ..utils.coordinator import MemoryCoordinator

            coord = MemoryCoordinator()
            coord.enqueue_write(messages, user_id=user_id)
            _hook_log(f"post_reply 完成: 异步写入已入队, agent={agent_name}")

        except Exception as exc:
            logger.warning("[Mem0] post_reply hook 失败: %s", exc)
        finally:
            # 重置所有标志, 准备下一轮对话
            self._reset_flags(agent)
        return None

    # ================================================================
    # 辅助方法
    # ================================================================

    def _reset_flags(self, agent: Any) -> None:
        """重置本轮所有去重标志和缓冲区。"""
        agent._mem0_searched = False
        agent._mem0_tool_collected = False
        agent._mem0_tool_buffer = []

    def _do_search(self, agent: Any, query: str, agent_name: str) -> None:
        """执行记忆检索并注入 Hint (被 pre_reply 和 pre_reasoning 共用)。"""
        user_id = agent_name or getattr(agent, "name", "default_user")
        _hook_log(f"检索: query='{query[:50]}', user_id={user_id}")

        from ..utils.coordinator import MemoryCoordinator

        coord = MemoryCoordinator()
        if not coord.is_available:
            _hook_log(f"检索跳过: Mem0 不可用, agent={agent_name}")
            return

        memories = coord.search(
            query=query,
            user_id=user_id,
            limit=5,
        )
        _hook_log(f"检索结果: {len(memories) if memories else 0} 条, agent={agent_name}")
        if not memories:
            return

        # 构建记忆文本块 (截断保护)
        lines = []
        for m in memories:
            if isinstance(m, dict):
                text = m.get("memory", "")
                if text:
                    lines.append(f"• {text}")
        if not lines:
            return

        block = "\n".join(lines)
        if len(block) > self.max_chars:
            block = block[: self.max_chars] + "..."

        hint_content = f"[用户长期记忆]\n{block}"

        # 以 Hint 消息注入短期记忆 (推理后自动清除)
        self._inject_hint(agent, hint_content)
        _hook_log(f"注入成功: {len(lines)} 条记忆, agent={agent_name}")
        logger.debug("[Mem0] 注入 %d 条记忆", len(lines))

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """从 Msg 对象提取纯文本内容。"""
        if msg is None:
            return ""
        if hasattr(msg, "get_text_content"):
            try:
                return msg.get_text_content() or ""
            except Exception:
                pass
        if isinstance(msg, str):
            return msg
        return str(msg)

    @staticmethod
    def _is_heartbeat(kwargs: dict[str, Any], output: Any) -> bool:
        """检测 QwenPaw 心跳任务。

        心跳任务不应被记忆。检测方式：
        1. kwargs 中显式标记 ``is_heartbeat=True``
        2. 消息内容包含 "heartbeat" 或 "心跳"
        3. 输出为空且消息极短
        """
        if kwargs.get("is_heartbeat", False):
            return True
        msg = kwargs.get("msg")
        if msg is not None:
            if isinstance(msg, list):
                text = MemoryHooks._extract_text(msg[-1]) if msg else ""
            else:
                text = MemoryHooks._extract_text(msg)
            if "heartbeat" in text.lower() or "心跳" in text:
                return True
        if output is None:
            return True
        return False

    @staticmethod
    def _inject_hint(agent: Any, content: str) -> None:
        """将记忆内容以 Hint 消息注入 Agent 短期记忆。

        使用 ``_MemoryMark.HINT`` 标记，``_reasoning`` 会在使用后
        自动清除，不会污染长期对话历史。
        """
        try:
            from agentscope.agent._react_agent import _MemoryMark
            from agentscope.message import Msg

            hint = Msg(name="mem0", content=content, role="system")
            # agent.memory 是 async 的
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在异步上下文中，创建 task 但不等待
                asyncio.ensure_future(
                    agent.memory.add(hint, marks=_MemoryMark.HINT),
                )
            else:
                loop.run_until_complete(
                    agent.memory.add(hint, marks=_MemoryMark.HINT),
                )
        except ImportError:
            # 回退方案：直接修改 system prompt
            sys_prompt = getattr(agent, "_sys_prompt", "") or ""
            agent._sys_prompt = f"{sys_prompt}\n\n{content}"
        except Exception as exc:
            logger.debug("[Mem0] Hint 注入失败，回退到 sys_prompt: %s", exc)
            sys_prompt = getattr(agent, "_sys_prompt", "") or ""
            agent._sys_prompt = f"{sys_prompt}\n\n{content}"
