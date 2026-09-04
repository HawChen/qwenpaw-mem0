"""Skill 增强层 — 5 个显式记忆管理工具。

所有函数使用标准 Google 风格 Docstring，供 AgentScope ``Toolkit``
解析并注册为 LLM 可调用的工具。Agent 可按需调用这些工具来
显式管理长期记忆 (搜索、列出、删除、更新、添加)。

防复活机制：``delete_memory`` 和 ``update_memory`` 会将旧内容
及其变体的 MD5 哈希加入 Coordinator 黑名单 (TTL=900s)，拦截
Hook 异步队列中的旧数据重写。
"""
# 注意: 不使用 from __future__ import annotations, 因为 pydantic 需要运行时类型解析

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("qwenpaw_mem0")


def search_memory(
    query: str,
    user_id: str = "default_user",
    limit: int = 5,
) -> Dict[str, Any]:
    """搜索长期记忆。根据查询文本检索相关的用户记忆。

    当需要回忆用户之前提到过的偏好、事实或历史信息时调用此工具。

    Args:
        query (str): 搜索查询文本，描述要查找的记忆内容。
        user_id (str): 用户唯一标识，默认 "default_user"。
        limit (int): 返回结果数量上限，默认 5。

    Returns:
        dict: 包含搜索结果的字典，格式为
            {"status": "success", "memories": [...], "count": int}。
            每条记忆包含 id、memory (文本)、score (相关度) 字段。
    """
    from ..utils.coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    if not coord.is_available:
        return {"status": "error", "message": "Mem0 服务不可用"}

    try:
        results = coord.search(query=query, user_id=user_id, limit=limit)
        return {
            "status": "success",
            "memories": results,
            "count": len(results),
        }
    except Exception as exc:
        logger.error("[Mem0] search_memory 失败: %s", exc)
        return {"status": "error", "message": str(exc)}


def list_all_memories(
    user_id: str = "default_user",
    limit: int = 100,
) -> Dict[str, Any]:
    """列出所有长期记忆。获取指定用户的全部记忆列表。

    当用户询问"你记住了我什么"或需要查看所有记忆时调用此工具。

    Args:
        user_id (str): 用户唯一标识，默认 "default_user"。
        limit (int): 返回数量上限，默认 100。

    Returns:
        dict: 包含记忆列表的字典，格式为
            {"status": "success", "memories": [...], "count": int}。
    """
    from ..utils.coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    if not coord.is_available:
        return {"status": "error", "message": "Mem0 服务不可用"}

    try:
        results = coord.list_all(user_id=user_id, limit=limit)
        return {
            "status": "success",
            "memories": results,
            "count": len(results),
        }
    except Exception as exc:
        logger.error("[Mem0] list_all_memories 失败: %s", exc)
        return {"status": "error", "message": str(exc)}


def delete_memory(
    memory_id: str,
    memory_content: str = "",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """删除指定的长期记忆。当用户明确要求"忘掉..."时调用。

    删除后会启用防复活保护：将旧内容及其变体的哈希加入黑名单，
    防止 Hook 异步队列中的旧数据重新写入该记忆。

    Args:
        memory_id (str): 要删除的记忆唯一 ID。
        memory_content (str): 记忆的原始文本内容 (用于防复活黑名单，
            强烈建议传入以启用防复活保护)。
        user_id (str): 用户唯一标识，默认 "default_user"。

    Returns:
        dict: 包含操作状态的字典，格式为
            {"status": "success", "message": "记忆已删除，已启用防复活保护"}。
    """
    from ..utils.coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    if not coord.is_available:
        return {"status": "error", "message": "Mem0 服务不可用"}

    try:
        # 防复活：将旧内容及其变体加入黑名单
        if memory_content:
            coord.blacklist_variants(memory_content)

        coord.delete(memory_id=memory_id)
        logger.info("[Mem0] 记忆已删除: %s (防复活保护已启用)", memory_id)
        return {
            "status": "success",
            "message": "记忆已删除，已启用防复活保护。",
            "memory_id": memory_id,
        }
    except Exception as exc:
        logger.error("[Mem0] delete_memory 失败: %s", exc)
        return {"status": "error", "message": str(exc)}


def update_memory(
    memory_id: str,
    new_content: str,
    old_content: str = "",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """更新指定的长期记忆内容。当用户要求修改已记住的信息时调用。

    更新旧内容后会启用防复活保护，防止异步队列中的旧数据覆盖更新。

    Args:
        memory_id (str): 要更新的记忆唯一 ID。
        new_content (str): 新的记忆文本内容。
        old_content (str): 旧的记忆文本内容 (用于防复活黑名单，
            建议传入以启用防复活保护)。
        user_id (str): 用户唯一标识，默认 "default_user"。

    Returns:
        dict: 包含操作状态的字典，格式为
            {"status": "success", "message": "记忆已更新"}。
    """
    from ..utils.coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    if not coord.is_available:
        return {"status": "error", "message": "Mem0 服务不可用"}

    try:
        # 防复活：将旧内容加入黑名单
        if old_content:
            coord.blacklist_variants(old_content)

        coord.update(memory_id=memory_id, text=new_content)
        logger.info("[Mem0] 记忆已更新: %s", memory_id)
        return {
            "status": "success",
            "message": "记忆已更新，已启用防复活保护。",
            "memory_id": memory_id,
            "new_content": new_content,
        }
    except Exception as exc:
        logger.error("[Mem0] update_memory 失败: %s", exc)
        return {"status": "error", "message": str(exc)}


def add_memory(
    content: str,
    user_id: str = "default_user",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """添加新的长期记忆。当需要显式记住某条信息时调用。

    Args:
        content (str): 要记住的记忆文本内容。
        user_id (str): 用户唯一标识，默认 "default_user"。
        metadata (dict, optional): 附加元数据，如来源、标签等。

    Returns:
        dict: 包含操作状态的字典，格式为
            {"status": "success", "message": "记忆已添加"}。
    """
    from ..utils.coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    if not coord.is_available:
        return {"status": "error", "message": "Mem0 服务不可用"}

    try:
        messages = [{"role": "user", "content": content}]
        coord.add(messages=messages, user_id=user_id, metadata=metadata)
        logger.info("[Mem0] 记忆已添加: %s", content[:50])
        return {
            "status": "success",
            "message": "记忆已添加。",
            "content": content,
        }
    except Exception as exc:
        logger.error("[Mem0] add_memory 失败: %s", exc)
        return {"status": "error", "message": str(exc)}


# 导出所有工具函数列表 (供 sitecustomize.py 注入时遍历)
ALL_TOOLS: List = [
    search_memory,
    list_all_memories,
    delete_memory,
    update_memory,
    add_memory,
]
