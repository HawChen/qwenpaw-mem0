"""协调器模块 — 记忆系统的心脏。

``MemoryCoordinator`` 是线程安全单例，被 ``hooks/`` 和 ``skills/`` 共享，
确保令牌桶限流、防复活黑名单和异步写入队列的全局唯一性。

核心设计：
- 基于 ``mem0.MemoryClient`` (HTTP 客户端) 对接火山引擎 Mem0 托管服务，
  而非本地 ``Memory`` 类 (mem0ai 0.1.118 的 VectorStoreFactory 不支持 volcengine)。
- 强制超时：通过自定义 ``httpx.Client(timeout=...)`` 防止网络黑洞卡死主进程。
- 令牌桶限流：保守设为 18 QPS (火山引擎硬限 20 QPS)。
- 指数退避：捕获 429 / AccountFlowLimitExceeded 后自动重试。
- 异步写入：后台守护线程消费队列，不阻塞 Agent 主循环。
- 防复活黑名单：Skill 层删除/更新记忆时，将旧内容哈希加入黑名单 (TTL=900s)，
  拦截 Hook 异步队列中的旧数据重写。
"""
from __future__ import annotations

import hashlib
import logging
import queue
import random
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import CONFIG

logger = logging.getLogger("qwenpaw_mem0")


class MemoryCoordinator:
    """线程安全单例协调器。

    封装火山引擎 Mem0 客户端的所有调用，统一处理超时、限流退避、
    异步写入和防复活黑名单。被 Hook 层和 Skill 层共享。
    """

    _instance: Optional["MemoryCoordinator"] = None
    _singleton_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 单例构造
    # ------------------------------------------------------------------
    def __new__(cls) -> "MemoryCoordinator":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # Mem0 客户端 (懒加载，避免构造时网络请求卡死)
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._client_error: Optional[str] = None

        # 防复活黑名单: {content_md5: expiry_timestamp}
        self._blacklist: Dict[str, float] = {}
        self._blacklist_lock = threading.Lock()

        # 异步写入队列
        self._queue: "queue.Queue[tuple]" = queue.Queue(
            maxsize=CONFIG.queue_max_size,
        )
        self._worker = threading.Thread(
            target=self._queue_worker,
            name="mem0-async-writer",
            daemon=True,
        )
        self._worker.start()

        # 令牌桶限流
        self._tokens: float = float(CONFIG.qps_limit)
        self._last_refill = time.time()
        self._bucket_lock = threading.Lock()

        logger.info(
            "MemoryCoordinator 初始化完成 (qps_limit=%s, timeout=%ss, "
            "blacklist_ttl=%ss, queue_max=%s)",
            CONFIG.qps_limit,
            CONFIG.request_timeout,
            CONFIG.blacklist_ttl,
            CONFIG.queue_max_size,
        )

    # ------------------------------------------------------------------
    # 客户端管理
    # ------------------------------------------------------------------
    def _get_client(self) -> Any:
        """懒加载 Mem0 MemoryClient (线程安全)。

        使用自定义 httpx.Client 设置强制超时，避免火山引擎网络黑洞。
        如果初始化失败 (凭据错误 / 网络不通)，返回 None 并记录错误，
        后续调用优雅降级而非崩溃。
        """
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                import httpx
                from mem0 import MemoryClient

                http_client = httpx.Client(
                    timeout=httpx.Timeout(
                        connect=CONFIG.request_timeout,
                        read=CONFIG.read_timeout,
                        write=CONFIG.request_timeout,
                        pool=CONFIG.request_timeout,
                    ),
                )
                self._client = MemoryClient(
                    api_key=CONFIG.volc_mem0_api_key,
                    host=CONFIG.volc_mem0_host,
                    client=http_client,
                )
                self._client_error = None
                logger.info(
                    "Mem0 MemoryClient 连接成功 (host=%s)",
                    CONFIG.volc_mem0_host,
                )
            except Exception as exc:
                self._client_error = str(exc)
                logger.error("Mem0 MemoryClient 初始化失败: %s", exc)
            return self._client

    @property
    def is_available(self) -> bool:
        """客户端是否可用 (已成功初始化)。"""
        return self._get_client() is not None

    # ------------------------------------------------------------------
    # 令牌桶限流
    # ------------------------------------------------------------------
    def _acquire_token(self) -> None:
        """令牌桶限流 — 阻塞直到获取一个令牌。

        令牌以 ``qps_limit`` 的速率补充，桶容量为 ``qps_limit``。
        当令牌不足时，计算需要等待的时间并 sleep。
        """
        while True:
            with self._bucket_lock:
                now = time.time()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(CONFIG.qps_limit),
                    self._tokens + elapsed * float(CONFIG.qps_limit),
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 计算补充 1 个令牌需要的时间
                deficit = 1.0 - self._tokens
                wait = deficit / float(CONFIG.qps_limit)
            time.sleep(wait)

    # ------------------------------------------------------------------
    # 统一重试封装
    # ------------------------------------------------------------------
    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        """判断异常是否为火山引擎限流 (429 / AccountFlowLimitExceeded)。"""
        text = str(exc)
        return (
            "429" in text
            or "AccountFlowLimitExceeded" in text
            or "RateLimit" in text
            or "TooManyRequests" in text
        )

    def _call_with_retry(
        self,
        func: Any,
        *args: Any,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> Any:
        """统一封装：令牌桶限流 + 限流指数退避重试。

        所有对火山引擎 API 的同步调用都应经过此方法。

        Args:
            func: MemoryClient 的方法 (search / add / delete 等)。
            *args: 位置参数。
            max_retries: 最大重试次数 (默认 3)。
            **kwargs: 关键字参数。

        Returns:
            API 返回值。

        Raises:
            RuntimeError: 重试耗尽仍失败。
            Exception: 非限流异常直接抛出。
        """
        client = self._get_client()
        if client is None:
            raise RuntimeError(
                f"Mem0 客户端不可用: {self._client_error}",
            )

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            self._acquire_token()
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if self._is_rate_limited(exc):
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[Mem0] 触发火山限流，退避 %.1fs (第 %d/%d 次)",
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait)
                    continue
                # 非限流异常直接抛出
                raise

        raise RuntimeError(
            f"Mem0 调用重试 {max_retries} 次后仍失败: {last_exc}",
        )

    # ------------------------------------------------------------------
    # 防复活黑名单
    # ------------------------------------------------------------------
    @staticmethod
    def hash_content(text: str) -> str:
        """计算文本内容的 MD5 哈希。"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def add_to_blacklist(
        self,
        hash_val: str,
        ttl: Optional[int] = None,
    ) -> None:
        """将哈希值加入黑名单。

        Skill 层的 delete/update 操作应将旧内容及其变体的哈希加入黑名单，
        防止 Hook 异步队列中的旧数据重写 (防复活)。

        Args:
            hash_val: 内容的 MD5 哈希。
            ttl: 黑名单存活秒数 (默认使用 CONFIG.blacklist_ttl)。
        """
        ttl = ttl if ttl is not None else CONFIG.blacklist_ttl
        with self._blacklist_lock:
            self._blacklist[hash_val] = time.time() + ttl

    def is_blacklisted(self, text: str) -> bool:
        """检查文本是否在黑名单中 (自动清理过期条目)。"""
        h = self.hash_content(text)
        with self._blacklist_lock:
            exp = self._blacklist.get(h)
            if exp is None:
                return False
            if time.time() > exp:
                del self._blacklist[h]
                return False
            return True

    def blacklist_variants(self, content: str, ttl: Optional[int] = None) -> None:
        """将内容及其常见变体全部加入黑名单。

        变体包括：原文、小写、带句号结尾，全面封杀异步队列中的旧数据。

        Args:
            content: 记忆的原始文本内容。
            ttl: 黑名单存活秒数。
        """
        variants = [
            content,
            content.lower(),
            content.strip(),
            content.rstrip("。.!?！？"),
            f"{content}。",
        ]
        for v in variants:
            if v:
                self.add_to_blacklist(self.hash_content(v), ttl=ttl)

    # ------------------------------------------------------------------
    # 异步写入队列
    # ------------------------------------------------------------------
    def enqueue_write(
        self,
        messages: List[Dict[str, str]],
        user_id: str = "default_user",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """将写入任务放入异步队列 (非阻塞)。

        Args:
            messages: 对话消息列表 [{"role": "user", "content": "..."}, ...]。
            user_id: 用户唯一标识。
            metadata: 附加元数据。

        Returns:
            True 入队成功，False 队列已满。
        """
        try:
            self._queue.put_nowait((messages, user_id, metadata))
            return True
        except queue.Full:
            logger.warning("[Mem0] 异步写入队列已满，丢弃本次写入")
            return False

    def _queue_worker(self) -> None:
        """后台守护线程：消费异步写入队列。

        对每条写入任务执行黑名单检查，避免被删除的记忆"复活"。
        """
        while True:
            messages, user_id, metadata = self._queue.get()
            try:
                # 拼接内容用于黑名单检查
                content = " ".join(
                    m.get("content", "")
                    for m in messages
                    if isinstance(m, dict)
                )
                if self.is_blacklisted(content):
                    logger.debug("[Mem0] 黑名单拦截，跳过异步写入")
                    continue

                self._call_with_retry(
                    self._get_client().add,
                    messages=messages,
                    user_id=user_id,
                    **({"metadata": metadata} if metadata else {}),
                )
                logger.info("[Hook] Memory saved asynchronously")
            except Exception as exc:
                logger.error("[Mem0] 异步写入失败: %s", exc)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # 公共 API (供 Skill 层和 Hook 层调用)
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        user_id: str = "default_user",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """检索记忆。

        Args:
            query: 检索查询文本。
            user_id: 用户唯一标识。
            limit: 返回结果数量上限。

        Returns:
            记忆列表，每项为 {"id": ..., "memory": ..., "score": ...}。
        """
        client = self._get_client()
        if client is None:
            return []
        results = self._call_with_retry(
            client.search,
            query=query,
            user_id=user_id,
            top_k=limit,
        )
        # MemoryClient.search 可能返回 list 或 {"results": [...]}
        if isinstance(results, dict) and "results" in results:
            return results["results"]
        return results or []

    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str = "default_user",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步写入记忆 (供 Skill 层显式调用)。

        Args:
            messages: 消息列表。
            user_id: 用户唯一标识。
            metadata: 附加元数据。

        Returns:
            API 返回结果。
        """
        client = self._get_client()
        if client is None:
            return {"status": "error", "message": "Mem0 客户端不可用"}
        return self._call_with_retry(
            client.add,
            messages=messages,
            user_id=user_id,
            **({"metadata": metadata} if metadata else {}),
        )

    def list_all(
        self,
        user_id: str = "default_user",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出所有记忆。

        Args:
            user_id: 用户唯一标识。
            limit: 返回数量上限。

        Returns:
            记忆列表。
        """
        client = self._get_client()
        if client is None:
            return []
        results = self._call_with_retry(
            client.get_all,
            user_id=user_id,
            top_k=limit,
        )
        if isinstance(results, dict) and "results" in results:
            return results["results"]
        return results or []

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """删除指定记忆。

        Args:
            memory_id: 记忆唯一 ID。

        Returns:
            API 返回结果。
        """
        client = self._get_client()
        if client is None:
            return {"status": "error", "message": "Mem0 客户端不可用"}
        return self._call_with_retry(client.delete, memory_id=memory_id)

    def update(
        self,
        memory_id: str,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """更新指定记忆。

        Args:
            memory_id: 记忆唯一 ID。
            text: 新的记忆文本内容。
            metadata: 新的元数据。

        Returns:
            API 返回结果。
        """
        client = self._get_client()
        if client is None:
            return {"status": "error", "message": "Mem0 客户端不可用"}
        return self._call_with_retry(
            client.update,
            memory_id=memory_id,
            text=text,
            **({"metadata": metadata} if metadata else {}),
        )

    def get(self, memory_id: str) -> Dict[str, Any]:
        """获取单条记忆详情。

        Args:
            memory_id: 记忆唯一 ID。

        Returns:
            记忆详情字典。
        """
        client = self._get_client()
        if client is None:
            return {}
        return self._call_with_retry(client.get, memory_id=memory_id)
