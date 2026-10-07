"""火山引擎 Mem0 REST 客户端（零第三方依赖，仅用内置 httpx）。

背景
----
QwenPaw 2.x 后端是 PyInstaller 冻结包，环境内没有 mem0ai（且缺
``qdrant_client`` / ``posthog`` / ``sqlalchemy``，无法补齐），
导致 ``from mem0 import MemoryClient`` 失败、整套记忆方案失效。
本模块用 httpx 直接对接 Mem0 REST API，移除 mem0ai 依赖。

契约来源
--------
逐行对照 ``mem0ai==0.1.118`` 的 ``mem0/client/main.py`` 提取，
并已对线上服务做过等价性验证（get_all 62=62 条、search 5/5 命中重叠、
add/get/update/delete 全链路通过）：

==============  ========  ==========================  ==============================
操作            方法      路径                        载荷 / 参数
==============  ========  ==========================  ==============================
校验            GET       /v1/ping/                   —
新增            POST      /v1/memories/               {messages, user_id, metadata?, version:"v2", output_format:"v1.1"}
检索            POST      /v1/memories/search/        {query, user_id, top_k}
列举            GET       /v1/memories/               user_id, top_k
详情            GET       /v1/memories/{id}/          —
更新            PUT       /v1/memories/{id}/          {text?, metadata?}
删除            DELETE    /v1/memories/{id}/          —
==============  ========  ==========================  ==============================

请求头（所有请求）::

    Authorization: Token <api_key>
    Mem0-User-ID: <md5(api_key)>

注意
----
服务端 ``add`` 是**异步**的（响应提示"3 分钟内完成"），写入后立即查询查不到
属正常行为，验证时须轮询等待。
"""
from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("qwenpaw_mem0")


class Mem0HttpError(RuntimeError):
    """Mem0 REST 调用失败。"""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class Mem0HttpClient:
    """极小化的 Mem0 REST 客户端，接口对齐 MemoryClient。

    仅依赖 httpx（QwenPaw 后端已内置），可在 PyInstaller 冻结环境中工作。
    内置令牌桶限流（火山引擎硬限 20 QPS）与限流退避重试。
    """

    def __init__(
        self,
        api_key: str,
        host: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        qps_limit: float = 18.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("Mem0 API Key 未提供")
        if not host:
            raise ValueError("Mem0 host 未提供")

        import httpx  # 延迟导入，便于在无 httpx 环境给出清晰报错

        self._api_key = api_key
        self._host = host.rstrip("/")
        self._org_id: Optional[str] = None
        self._project_id: Optional[str] = None
        self._max_retries = max_retries

        # 令牌桶限流
        self._qps = max(qps_limit, 1.0)
        self._tokens = self._qps
        self._last_refill = time.time()
        self._bucket_lock = threading.Lock()

        self._client = httpx.Client(
            base_url=self._host,
            headers={
                "Authorization": f"Token {api_key}",
                "Mem0-User-ID": hashlib.md5(api_key.encode()).hexdigest(),
            },
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=connect_timeout,
                pool=connect_timeout,
            ),
        )

    # ------------------------------------------------------------------
    # 限流与重试
    # ------------------------------------------------------------------
    def _acquire_token(self) -> None:
        """令牌桶限流：阻塞直到取到一个令牌。"""
        while True:
            with self._bucket_lock:
                now = time.time()
                self._tokens = min(
                    self._qps,
                    self._tokens + (now - self._last_refill) * self._qps,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._qps
            time.sleep(wait)

    @staticmethod
    def _is_rate_limited(resp: Any, exc: Exception) -> bool:
        if getattr(resp, "status_code", 0) == 429:
            return True
        text = str(exc)
        return any(
            k in text
            for k in ("429", "AccountFlowLimitExceeded", "RateLimit", "TooManyRequests")
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """统一出口：令牌桶 + 限流退避重试 + 状态码校验。"""
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            self._acquire_token()
            resp = None
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code == 429:
                    raise Mem0HttpError(429, resp.text[:200])
                self._raise_for_status(resp)
                return resp
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if self._is_rate_limited(resp, exc) and attempt < self._max_retries - 1:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[Mem0] 触发限流，退避 %.1fs（第 %d/%d 次）",
                        wait, attempt + 1, self._max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise
        raise Mem0HttpError(0, f"重试 {self._max_retries} 次后仍失败: {last_exc}")

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造查询参数（自动带上 ping 得到的 org_id / project_id）。"""
        p: Dict[str, Any] = dict(extra or {})
        if self._org_id and self._project_id:
            p["org_id"] = self._org_id
            p["project_id"] = self._project_id
        return {k: v for k, v in p.items() if v is not None}

    @staticmethod
    def _raise_for_status(resp: Any) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text or "<空响应体>"
        raise Mem0HttpError(resp.status_code, str(detail)[:400])

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def ping(self) -> Dict[str, Any]:
        """校验 API Key 并缓存 org_id / project_id。"""
        resp = self._request("GET", "/v1/ping/", params=self._params())
        data = resp.json()
        if data.get("org_id") and data.get("project_id"):
            self._org_id = data.get("org_id")
            self._project_id = data.get("project_id")
        return data

    # ------------------------------------------------------------------
    # 记忆操作
    # ------------------------------------------------------------------
    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """新增记忆（服务端会做 LLM 抽取，且为异步落库）。"""
        payload: Dict[str, Any] = {
            "messages": messages,
            "user_id": user_id,
            "version": "v2",
            "output_format": "v1.1",
        }
        if metadata:
            payload["metadata"] = metadata
        resp = self._request("POST", "/v1/memories/", json=payload, params=self._params())
        return resp.json()

    def search(
        self,
        query: str,
        user_id: str = "default",
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """语义检索记忆，返回结果列表。"""
        resp = self._request(
            "POST",
            "/v1/memories/search/",
            json={"query": query, "user_id": user_id, "top_k": top_k},
            params=self._params(),
        )
        return self._as_list(resp.json())

    def get_all(
        self,
        user_id: str = "default",
        top_k: int = 100,
    ) -> List[Dict[str, Any]]:
        """列举某个 user_id 下的记忆。"""
        resp = self._request(
            "GET",
            "/v1/memories/",
            params=self._params({"user_id": user_id, "top_k": top_k}),
        )
        return self._as_list(resp.json())

    def get(self, memory_id: str) -> Dict[str, Any]:
        """获取单条记忆详情。"""
        resp = self._request("GET", f"/v1/memories/{memory_id}/", params=self._params())
        return resp.json()

    def update(
        self,
        memory_id: str,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """更新单条记忆。"""
        if text is None and metadata is None:
            raise ValueError("text 与 metadata 至少要提供一个")
        payload: Dict[str, Any] = {}
        if text is not None:
            payload["text"] = text
        if metadata is not None:
            payload["metadata"] = metadata
        resp = self._request(
            "PUT", f"/v1/memories/{memory_id}/", json=payload, params=self._params()
        )
        return resp.json()

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """删除单条记忆。"""
        resp = self._request("DELETE", f"/v1/memories/{memory_id}/", params=self._params())
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}

    # ------------------------------------------------------------------
    @staticmethod
    def _as_list(data: Any) -> List[Dict[str, Any]]:
        """兼容 list 与 {"results": [...]} 两种返回结构。"""
        if isinstance(data, dict) and "results" in data:
            return data["results"] or []
        return data or []

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass