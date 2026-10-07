"""5 个记忆工具（Skill 增强层）。

由 ``Mem0MemoryManager.list_memory_tools()`` 交给框架注册，Agent 可在需要时
显式调用。对应旧方案的 5 个 Skill 工具，语义保持一致。

设计约束
--------
1. **不使用** ``from __future__ import annotations`` —— 该导入会让注解变成字符串，
   导致 pydantic 无法解析类型、工具注册失败（旧方案已验证过的坑）。
2. 参数类型只用 ``str`` / ``int``，不用 ``Dict`` / ``Optional`` ——
   避免 JSON Schema 生成失败；标签用逗号分隔字符串表达。
"""
import json
import logging
from typing import Any, List

logger = logging.getLogger("qwenpaw_mem0")


def _fmt_items(items: List[dict]) -> str:
    """把记忆列表格式化为紧凑文本，便于 LLM 阅读。"""
    if not items:
        return "（无结果）"
    lines = []
    for i, it in enumerate(items, 1):
        mid = it.get("id", "?")
        text = it.get("memory") or it.get("text") or ""
        score = it.get("score")
        suffix = f"  [相似度 {score:.2f}]" if isinstance(score, (int, float)) else ""
        lines.append(f"{i}. {text}\n   id: {mid}{suffix}")
    return "\n".join(lines)


class MemoryTools:
    """持有 Mem0 客户端与 user_id，向 Agent 暴露记忆管理工具。

    实例由 ``Mem0MemoryManager`` 创建，``list_memory_tools()`` 返回其绑定方法。
    """

    def __init__(
        self,
        client: Any,
        user_id: str,
        meter: Any = None,
        dedup: Any = None,
    ) -> None:
        self._client = client
        self._user_id = user_id
        self._meter = meter
        self._dedup = dedup

    # ------------------------------------------------------------------
    def _record(self, kind: str, chars: int) -> None:
        if self._meter is not None:
            try:
                self._meter.record(kind, chars)
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    # 工具实现
    # ==================================================================
    def search_memory(self, query: str, limit: int = 5) -> str:
        """检索长期记忆。

        当需要回忆之前的对话内容、用户偏好、决定、日期、人物或待办时使用。

        Args:
            query: 检索关键词或问题，尽量具体。
            limit: 返回条数上限，默认 5。

        Returns:
            匹配到的记忆列表（含 id 与相似度）。
        """
        if not query or not query.strip():
            return "检索失败：query 不能为空。"
        try:
            top_k = max(1, min(int(limit or 5), 20))
        except (TypeError, ValueError):
            top_k = 5
        try:
            results = self._client.search(
                query=query.strip(), user_id=self._user_id, top_k=top_k
            )
            self._record("search_manual", len(query))
            return _fmt_items(results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] search_memory 工具失败: %s", exc)
            return f"检索失败：{exc}"

    def add_memory(self, content: str, tags: str = "") -> str:
        """显式写入一条长期记忆。

        用于明确要长期记住的事实、偏好、承诺或日程，避免自动抽取遗漏。

        Args:
            content: 要记住的内容，用完整的一句话表述。
            tags: 可选标签，多个用逗号分隔，例如 "饮食,偏好"。

        Returns:
            写入结果说明。
        """
        if not content or not content.strip():
            return "写入失败：content 不能为空。"
        text = content.strip()
        metadata: dict = {"source": "explicit_tool"}
        if tags and tags.strip():
            metadata["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        try:
            result = self._client.add(
                messages=[
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": "已记录。"},
                ],
                user_id=self._user_id,
                metadata=metadata,
            )
            self._record("add_manual", len(text))
            ok = True
            if isinstance(result, dict) and result.get("status") == "error":
                ok = False
            if ok:
                return (
                    f"已提交写入（内容：{text}）。\n"
                    "注意：服务端异步落库，约 1-3 分钟后才可检索到。"
                )
            return f"写入返回异常：{json.dumps(result, ensure_ascii=False)[:200]}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] add_memory 工具失败: %s", exc)
            return f"写入失败：{exc}"

    def list_memories(self, limit: int = 20) -> str:
        """列出当前 Agent 已保存的记忆。

        用于盘点记忆内容、找出需要清理或修正的条目（拿到 id 后可配合
        update_memory / delete_memory 使用）。

        Args:
            limit: 返回条数上限，默认 20。

        Returns:
            记忆条目列表（含 id）。
        """
        try:
            top_k = max(1, min(int(limit or 20), 200))
        except (TypeError, ValueError):
            top_k = 20
        try:
            results = self._client.get_all(user_id=self._user_id, top_k=top_k)
            self._record("list_manual", 0)
            return _fmt_items(results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] list_memories 工具失败: %s", exc)
            return f"列举失败：{exc}"

    def update_memory(self, memory_id: str, content: str) -> str:
        """修改一条已有记忆的内容。

        用于修正错误记录、补全信息或更新已过期的内容。
        建议先用 search_memory / list_memories 拿到 memory_id。

        Args:
            memory_id: 要修改的记忆 id。
            content: 新的完整内容。

        Returns:
            更新结果说明。
        """
        if not memory_id or not memory_id.strip():
            return "更新失败：memory_id 不能为空。"
        if not content or not content.strip():
            return "更新失败：content 不能为空。"
        try:
            self._client.update(memory_id.strip(), text=content.strip())
            self._record("update_manual", len(content))
            return f"已更新记忆 {memory_id.strip()}。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] update_memory 工具失败: %s", exc)
            return f"更新失败：{exc}"

    def delete_memory(self, memory_id: str) -> str:
        """删除一条记忆。

        用于清除错误、过期或不应保留的记忆。
        建议先用 search_memory / list_memories 拿到 memory_id。

        Args:
            memory_id: 要删除的记忆 id。

        Returns:
            删除结果说明。
        """
        if not memory_id or not memory_id.strip():
            return "删除失败：memory_id 不能为空。"
        mid = memory_id.strip()
        try:
            # 先把内容加入去重黑名单，避免异步写入把它复活
            if self._dedup is not None:
                try:
                    detail = self._client.get(mid)
                    text = detail.get("memory") or detail.get("text") or ""
                    if text:
                        self._dedup.mark_deleted(text)
                except Exception:  # noqa: BLE001
                    pass
            self._client.delete(mid)
            self._record("delete_manual", 0)
            return f"已删除记忆 {mid}。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] delete_memory 工具失败: %s", exc)
            return f"删除失败：{exc}"

    # ------------------------------------------------------------------
    def all_tools(self) -> List[Any]:
        """按固定顺序返回 5 个工具（绑定方法）。"""
        return [
            self.search_memory,
            self.add_memory,
            self.list_memories,
            self.update_memory,
            self.delete_memory,
        ]