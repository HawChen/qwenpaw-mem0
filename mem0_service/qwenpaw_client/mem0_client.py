"""QwenPaw 侧客户端 — Mem0ServiceClient + ServiceToolkitAdapter。

提供两种集成模式：

1. **进程内模式** (推荐，由 sitecustomize.py 自动注入)：
   ``ServiceToolkitAdapter.register_to_toolkit(toolkit)`` 将 5 个记忆
   工具注册到 Agent 的 Toolkit 上，Hook 通过 ``register_class_hook``
   自动注册，无需此客户端。

2. **HTTP 服务模式** (可选)：
   ``Mem0ServiceClient`` 作为轻量 HTTP 客户端，连接独立运行的
   FastAPI 服务 (``api/routes.py``)，适用于多进程解耦场景。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("qwenpaw_mem0")


class Mem0ServiceClient:
    """轻量 HTTP 客户端 — 连接独立运行的 Mem0 FastAPI 服务。

    当 Mem0 服务作为独立进程运行 (而非进程内注入) 时使用。
    进程内模式下无需此类，直接使用 ``MemoryCoordinator``。

    Args:
        base_url: FastAPI 服务地址 (如 http://127.0.0.1:8765)。
        timeout: 请求超时秒数。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
        )

    def search(
        self,
        query: str,
        user_id: str = "default_user",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        resp = self._client.post(
            "/memories/search",
            json={"query": query, "user_id": user_id, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("memories", [])

    def add(
        self,
        content: str,
        user_id: str = "default_user",
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        resp = self._client.post(
            "/memories",
            json={
                "content": content,
                "user_id": user_id,
                "metadata": metadata,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def list_all(
        self,
        user_id: str = "default_user",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        resp = self._client.get(
            "/memories",
            params={"user_id": user_id, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("memories", [])

    def delete(self, memory_id: str) -> Dict[str, Any]:
        resp = self._client.delete(f"/memories/{memory_id}")
        resp.raise_for_status()
        return resp.json()

    def update(
        self,
        memory_id: str,
        text: str,
    ) -> Dict[str, Any]:
        resp = self._client.put(
            f"/memories/{memory_id}",
            json={"text": text},
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()


class ServiceToolkitAdapter:
    """将 Mem0 记忆工具适配到 AgentScope Toolkit。

    在进程内模式下，``register_to_toolkit`` 将 5 个记忆管理函数
    注册到 Agent 的 Toolkit 实例上，使 LLM 可按需调用。

    使用示例::

        from agentscope.tool import Toolkit
        toolkit = Toolkit()
        ServiceToolkitAdapter.register_to_toolkit(toolkit)
    """

    @staticmethod
    def register_to_toolkit(
        toolkit: Any,
        namesake_strategy: str = "skip",
    ) -> List[str]:
        """将 5 个记忆工具注册到 Toolkit。

        Args:
            toolkit: AgentScope Toolkit 实例。
            namesake_strategy: 重名策略 ("skip" / "override" / "raise" / "rename")。

        Returns:
            成功注册的工具名称列表。
        """
        from ..skills.memory_skills import ALL_TOOLS

        registered: List[str] = []
        for tool_fn in ALL_TOOLS:
            try:
                toolkit.register_tool_function(
                    tool_fn,
                    namesake_strategy=namesake_strategy,
                )
                registered.append(tool_fn.__name__)
                logger.debug("[Mem0] 注册工具: %s", tool_fn.__name__)
            except Exception as exc:
                logger.warning(
                    "[Mem0] 注册工具 %s 失败: %s",
                    tool_fn.__name__,
                    exc,
                )
        logger.info("[Mem0] ServiceToolkitAdapter 注册 %d 个工具", len(registered))
        return registered
