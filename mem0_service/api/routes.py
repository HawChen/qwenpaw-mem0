"""FastAPI HTTP 路由层 — 可选的独立服务模式。

当需要将 Mem0 服务作为独立进程运行时，可启动此 FastAPI 服务。
进程内注入模式 (sitecustomize.py) 无需启动此服务。

启动方式::

    uvicorn qwenpaw_mem0_service.api.routes:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("qwenpaw_mem0")

router = APIRouter(prefix="/memories", tags=["mem0"])


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    user_id: str = "default_user"
    limit: int = 5


class AddRequest(BaseModel):
    content: str
    user_id: str = "default_user"
    metadata: Optional[Dict[str, Any]] = None


class UpdateRequest(BaseModel):
    text: str
    old_content: str = ""


class ListParams:
    def __init__(self, user_id: str = "default_user", limit: int = 100):
        self.user_id = user_id
        self.limit = limit


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------
def _get_coord():
    """懒加载 Coordinator (避免 import 时初始化)。"""
    from ..utils.coordinator import MemoryCoordinator

    return MemoryCoordinator()


@router.post("/search")
async def search_memories(req: SearchRequest) -> Dict[str, Any]:
    """搜索记忆。"""
    coord = _get_coord()
    if not coord.is_available:
        raise HTTPException(503, "Mem0 服务不可用")
    results = coord.search(query=req.query, user_id=req.user_id, limit=req.limit)
    return {"status": "success", "memories": results, "count": len(results)}


@router.get("")
async def list_memories(user_id: str = "default_user", limit: int = 100) -> Dict[str, Any]:
    """列出所有记忆。"""
    coord = _get_coord()
    if not coord.is_available:
        raise HTTPException(503, "Mem0 服务不可用")
    results = coord.list_all(user_id=user_id, limit=limit)
    return {"status": "success", "memories": results, "count": len(results)}


@router.post("")
async def add_memory(req: AddRequest) -> Dict[str, Any]:
    """添加记忆。"""
    coord = _get_coord()
    if not coord.is_available:
        raise HTTPException(503, "Mem0 服务不可用")
    messages = [{"role": "user", "content": req.content}]
    result = coord.add(messages=messages, user_id=req.user_id, metadata=req.metadata)
    return {"status": "success", "result": result}


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    memory_content: str = "",
) -> Dict[str, Any]:
    """删除记忆 (含防复活保护)。"""
    coord = _get_coord()
    if not coord.is_available:
        raise HTTPException(503, "Mem0 服务不可用")
    if memory_content:
        coord.blacklist_variants(memory_content)
    result = coord.delete(memory_id=memory_id)
    return {"status": "success", "result": result}


@router.put("/{memory_id}")
async def update_memory(
    memory_id: str,
    req: UpdateRequest,
) -> Dict[str, Any]:
    """更新记忆 (含防复活保护)。"""
    coord = _get_coord()
    if not coord.is_available:
        raise HTTPException(503, "Mem0 服务不可用")
    if req.old_content:
        coord.blacklist_variants(req.old_content)
    result = coord.update(memory_id=memory_id, text=req.text)
    return {"status": "success", "result": result}


# 可选: 创建 app 实例用于 uvicorn 启动
def create_app():
    """创建 FastAPI 应用 (用于独立服务模式)。"""
    from fastapi import FastAPI

    app = FastAPI(title="QwenPaw Mem0 Service", version="1.0.0")
    app.include_router(router)
    return app


app = create_app()
