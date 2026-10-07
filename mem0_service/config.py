"""全局配置（QwenPaw 2.x 版）。

自实现 ``.env`` 解析，**不依赖 python-dotenv** —— 冻结运行时缺包曾导致整套方案
失效（mem0ai 缺失），故此处只依赖标准库，彻底消除同类风险。

配置项分三类：
1. 火山引擎 Mem0 连接（host / api_key / 超时）
2. 降本参数（写入节奏、检索条数、写入门槛、工具输出开关）
3. 费用护栏（月度预算、Credit 换算系数、用量文件）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_env_file(path: Path) -> Dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 .env（跳过注释与空行，去除包裹引号）。"""
    data: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


_ENV_FILE = _load_env_file(_ENV_PATH)


def _get(key: str, default: str = "") -> str:
    """读取配置：优先环境变量，其次 .env 文件。"""
    return (os.getenv(key) or _ENV_FILE.get(key) or default).strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, "") or default)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(_get(key, "") or default)
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    """全局配置单例（不可变）。"""

    # ----- 火山引擎 Mem0 连接 -----
    volc_mem0_host: str = _get("VOLC_MEM0_HOST", "")
    volc_mem0_api_key: str = _get("VOLC_MEM0_API_KEY", "")
    connect_timeout: float = float(_get_int("MEM0_REQUEST_TIMEOUT", 5))
    read_timeout: float = float(_get_int("MEM0_READ_TIMEOUT", 15))
    qps_limit: float = _get_float("MEM0_QPS_LIMIT", 18.0)

    # ----- 降本参数（平衡档）-----
    auto_memory_interval: int = _get_int("MEM0_AUTO_MEMORY_INTERVAL", 5)
    max_results: int = _get_int("MEM0_MAX_RESULTS", 3)
    # 与旧方案保持一致：只拦真正琐碎的内容，"寒暄/报错"由 content_filter 的
    # is_noise() 负责，避免过高的长度门槛误杀短事实（如"我喜欢吃蔬菜"）
    min_write_length: int = _get_int("MEM0_MIN_WRITE_LENGTH", 10)
    keep_tool_messages: bool = _get_bool("MEM0_KEEP_TOOL_MESSAGES", False)
    memory_search_enabled: bool = _get_bool("MEM0_MEMORY_SEARCH_ENABLED", True)

    # ----- 费用护栏 -----
    usage_enabled: bool = _get_bool("MEM0_USAGE_ENABLED", True)
    monthly_budget_yuan: float = _get_float("MEM0_MONTHLY_BUDGET_YUAN", 10.0)
    credit_per_token: float = _get_float("MEM0_CREDIT_PER_TOKEN", 3.2)
    yuan_per_million_credit: float = _get_float("MEM0_YUAN_PER_MILLION_CREDIT", 1.12)
    # 防复活黑名单 TTL（秒）
    blacklist_ttl: int = _get_int("MEM0_BLACKLIST_TTL", 900)

    @property
    def usage_file(self) -> Path:
        """用量记账文件（与插件同目录，随备份同步）。"""
        return _ENV_PATH.parent / "usage.json"

    def validate(self) -> List[str]:
        """校验关键配置，返回错误信息列表（空列表表示通过）。"""
        errors: List[str] = []
        if not self.volc_mem0_api_key:
            errors.append("VOLC_MEM0_API_KEY 未配置，请在 mem0_service/.env 中填入")
        if not self.volc_mem0_host:
            errors.append("VOLC_MEM0_HOST 未配置")
        if self.qps_limit > 20:
            errors.append(f"MEM0_QPS_LIMIT={self.qps_limit} 超过火山引擎硬限 20 QPS")
        if self.auto_memory_interval < 0:
            errors.append("MEM0_AUTO_MEMORY_INTERVAL 不能为负")
        if self.max_results < 1:
            errors.append("MEM0_MAX_RESULTS 至少为 1")
        return errors


CONFIG = Config()