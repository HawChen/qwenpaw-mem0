"""写入前置过滤与去重（降本核心）。

原方案每轮无条件写入，且把工具调用结果全量合并进 ``add`` 的输入，
你跑日报 / 浏览器自动化时单次工具输出动辄数万字符 —— 这些都会变成
抽取 LLM 的输入 token，是最大的费用放大器。

本模块做三件事：
1. **丢弃 tool 角色消息** —— 工具输出通常是可再生的过程数据，不是"用户事实"
2. **挡掉噪音** —— 寒暄 / 纯确认 / 报错回显 / 心跳，不触发完整 LLM 抽取
3. **内容去重** —— 同一内容在 TTL 内不重复写入
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger("qwenpaw_mem0")

# ---------------------------------------------------------------------------
# 噪音判定
# ---------------------------------------------------------------------------
# 仅在"整段文本很短"时才判定为噪音，避免误杀
# "好的，我的邮箱是 x@y.com" 这类含有效信息的短句
_NOISE_EXACT = re.compile(
    r"^(好的?|好嘞|嗯+|哦+|收到|谢谢|感谢|多谢|ok|okay|okey|行|可以|没问题|"
    r"继续|明白|了解|知道了|是的|对的|对|没错|在吗|哈+|哈哈+|"
    r"hi|hello|hey|thanks|thank you|got it|sure|yes|no|yep|nope)"
    r"[\s。.!！?？,，~～]*$",
    re.IGNORECASE,
)

# 报错回显：这类内容不应进入长期记忆
_ERROR_ECHO = re.compile(
    r"(Traceback \(most recent call last\)|"
    r"\b(ModuleNotFoundError|ImportError|AttributeError|TypeError|ValueError|"
    r"KeyError|IndexError|RuntimeError|PermissionError|FileNotFoundError|"
    r"WinError \d+)\b|"
    r"拒绝访问|报错|异常堆栈)",
    re.IGNORECASE,
)

# 心跳 / 系统噪音
_HEARTBEAT = re.compile(r"(heartbeat|心跳|auto[_-]?dream)", re.IGNORECASE)

# 短文本阈值：低于此长度才启用"整段即噪音"判定
_NOISE_MAX_LEN = 40


def is_noise(text: str) -> bool:
    """判断文本是否为无记忆价值的噪音。"""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # 纯标点 / 纯空白
    if not re.search(r"[\w\u4e00-\u9fff]", stripped):
        return True
    if _HEARTBEAT.search(stripped):
        return True
    if _ERROR_ECHO.search(stripped):
        return True
    if len(stripped) <= _NOISE_MAX_LEN and _NOISE_EXACT.match(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------
def strip_tool_messages(
    messages: List[Dict[str, str]],
    keep_tool: bool = False,
) -> List[Dict[str, str]]:
    """丢弃 tool 角色消息（降本开关：``keep_tool=True`` 时保留）。"""
    if keep_tool:
        return list(messages)
    kept = [m for m in messages if (m.get("role") or "").lower() != "tool"]
    dropped = len(messages) - len(kept)
    if dropped:
        logger.debug("[Mem0] 已丢弃 %d 条 tool 消息，避免工具输出计入抽取 token", dropped)
    return kept


def should_write(
    messages: List[Dict[str, str]],
    min_length: int,
) -> bool:
    """判定这批消息是否值得写入。"""
    if not messages:
        return False
    parts = [str(m.get("content") or "") for m in messages]
    joined = " ".join(p for p in parts if p).strip()
    if len(joined) < min_length:
        return False
    # 全部内容都是噪音 → 不写
    if all(is_noise(p) for p in parts if p):
        return False
    return True


# ---------------------------------------------------------------------------
# 内容去重（进程内 TTL 缓存）
# ---------------------------------------------------------------------------
class DedupCache:
    """同一内容在 TTL 内只允许写入一次（线程安全）。"""

    def __init__(self, ttl: int = 900, max_entries: int = 2000) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def is_duplicate(self, text: str) -> bool:
        """若内容在 TTL 内已写入过，返回 True 并续期。"""
        h = self._hash(text)
        now = time.time()
        with self._lock:
            self._purge(now)
            exp = self._seen.get(h)
            if exp is not None and exp > now:
                return True
            self._seen[h] = now + self._ttl
            return False

    def mark_deleted(self, text: str) -> None:
        """将内容标记为已删除，阻止异步队列把它复活（防复活）。"""
        h = self._hash(text)
        with self._lock:
            self._seen[h] = time.time() + self._ttl

    def _purge(self, now: float) -> None:
        expired = [k for k, v in self._seen.items() if v <= now]
        for k in expired:
            del self._seen[k]
        # 兜底：超量时清掉最早的一批
        if len(self._seen) > self._max:
            for k in list(self._seen)[: len(self._seen) - self._max]:
                del self._seen[k]


def normalize_text(value: Any) -> str:
    """从各种可能的消息对象中提取纯文本（带兜底链）。

    兼容 agentscope 2.x 的 Msg / ContentBlock 结构与纯 str / dict。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value

    # 1) 2.x 可能保留的方法
    getter = getattr(value, "get_text_content", None)
    if callable(getter):
        try:
            text = getter()
            if text:
                return str(text)
        except Exception:
            pass

    # 2) content 属性：可能是 str、list[ContentBlock]、dict
    content = getattr(value, "content", None)
    if content is not None:
        return normalize_text(content)

    # 3) 2.x 新增块类型 / dict 结构
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return normalize_text(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(normalize_text(v) for v in value)

    # 4) text 属性 / 兜底
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    return str(value)