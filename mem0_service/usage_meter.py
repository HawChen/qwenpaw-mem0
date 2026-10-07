"""用量记账与费用熔断（11 月计费后的安全垫）。

2026-11-02 10:00（UTC+8）起火山引擎 Mem0 正式计费：

- Credit 消耗：1.12 元/百万 Credit（LLM 输入/输出 + Embedding token，主要成本项）
- 记忆存储：0.0025 元/万条·小时（个人量级可忽略）

本模块按字符数估算 token，累计 Credit 与金额到 ``usage.json``（按月分桶），
并在超过月度预算时**只停止写入**（检索仍放行，因为读远比写便宜）。

已知局限
--------
估算基于字符数而非服务端真实计量，用于**趋势判断与防意外**，不能替代控制台
账单。换算系数 ``MEM0_CREDIT_PER_TOKEN`` 建议在首个计费月后按真实账单校准。
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("qwenpaw_mem0")

# 兜底字符→token 换算（中英混合经验值）
_DEFAULT_TOKEN_DIVISOR = 1.6


class UsageMeter:
    """线程安全的用量记账与预算熔断。"""

    def __init__(
        self,
        usage_file: Path,
        *,
        credit_per_token: float,
        yuan_per_million_credit: float,
        monthly_budget_yuan: float,
        enabled: bool = True,
        token_divisor: Optional[float] = None,
    ) -> None:
        self._file = usage_file
        self._credit_per_token = credit_per_token
        self._yuan_per_million = yuan_per_million_credit
        self._budget = monthly_budget_yuan
        self._enabled = enabled
        self._divisor = token_divisor or _DEFAULT_TOKEN_DIVISOR
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()
        self._throttled_logged = False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _month_key() -> str:
        return datetime.datetime.now().strftime("%Y-%m")

    def _load(self) -> Dict[str, Any]:
        try:
            raw = self._file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Mem0] usage.json 读取失败，将重建: %s", exc)
        return {}

    def _bucket(self) -> Dict[str, Any]:
        key = self._month_key()
        bucket = self._data.get(key)
        if not isinstance(bucket, dict):
            bucket = {"calls": 0, "tokens": 0, "credits": 0.0, "yuan": 0.0, "by_kind": {}}
            self._data[key] = bucket
        bucket.setdefault("by_kind", {})
        return bucket

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._file)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Mem0] usage.json 写入失败: %s", exc)

    def set_token_divisor(self, divisor: Any) -> None:
        """由框架的 ``MemoryBackendContext.token_estimate_divisor`` 覆盖默认值。"""
        try:
            value = float(divisor)
            if value > 0:
                self._divisor = value
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    def record(self, kind: str, chars: int) -> None:
        """记录一次调用。``kind`` 形如 ``add`` / ``search`` / ``get_all``。"""
        if not self._enabled:
            return
        try:
            tokens = max(int(chars / self._divisor), 1)
            credits = tokens * self._credit_per_token
            yuan = credits / 1_000_000 * self._yuan_per_million
        except Exception:  # noqa: BLE001
            return

        with self._lock:
            b = self._bucket()
            b["calls"] += 1
            b["tokens"] += tokens
            b["credits"] = round(b["credits"] + credits, 2)
            b["yuan"] = round(b["yuan"] + yuan, 6)
            kind_stat = b["by_kind"].setdefault(kind, {"calls": 0, "tokens": 0})
            kind_stat["calls"] += 1
            kind_stat["tokens"] += tokens
            self._save()

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------
    def is_write_allowed(self) -> bool:
        """本月估算金额是否仍在预算内（熔断只作用于写入）。"""
        if not self._enabled or self._budget <= 0:
            return True
        with self._lock:
            spent = float(self._bucket().get("yuan") or 0.0)
        if spent >= self._budget:
            if not self._throttled_logged:
                logger.warning(
                    "[Mem0] 月度预算已用尽（估算 %.4f 元 / 预算 %.2f 元），"
                    "已暂停记忆写入；检索不受影响。如需继续，请调高 "
                    "MEM0_MONTHLY_BUDGET_YUAN。",
                    spent, self._budget,
                )
                self._throttled_logged = True
            return False
        self._throttled_logged = False
        return True

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """返回当前用量快照（供 get_runtime_status 使用）。"""
        with self._lock:
            b = dict(self._bucket())
        spent = float(b.get("yuan") or 0.0)
        return {
            "month": self._month_key(),
            "calls": b.get("calls", 0),
            "est_tokens": b.get("tokens", 0),
            "est_credits": b.get("credits", 0),
            "est_yuan": round(spent, 6),
            "budget_yuan": self._budget,
            "throttled": bool(self._budget > 0 and spent >= self._budget),
            "by_kind": b.get("by_kind", {}),
            "note": "估算值，非服务端真实计量；请以火山引擎控制台账单为准",
        }