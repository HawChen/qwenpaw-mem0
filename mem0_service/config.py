"""
全局配置加载模块
================
基于 dotenv 从 .env 加载火山引擎 Mem0 服务所需的所有配置。
所有模块统一通过此处的 Config 单例获取配置，避免散落的硬编码。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env (项目根目录)
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _get_env(key: str, default: str = "") -> str:
    """读取环境变量，去除首尾空白。"""
    return os.getenv(key, default).strip()


def _get_env_int(key: str, default: int) -> int:
    """读取环境变量并转为 int，转换失败回退 default。"""
    raw = _get_env(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    """读取环境变量并转为 float，转换失败回退 default。"""
    raw = _get_env(key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """全局配置单例 (不可变)。"""

    # ----- 火山引擎 Mem0 -----
    volc_mem0_host: str = _get_env("VOLC_MEM0_HOST", "https://mem0-cn-beijing.volces.com")
    volc_mem0_api_key: str = _get_env("VOLC_MEM0_API_KEY", "")

    # ----- 服务运行 -----
    service_host: str = _get_env("MEM0_SERVICE_HOST", "127.0.0.1")
    service_port: int = _get_env_int("MEM0_SERVICE_PORT", 8765)

    # ----- 协调器 -----
    qps_limit: float = _get_env_float("MEM0_QPS_LIMIT", 18.0)
    request_timeout: int = _get_env_int("MEM0_REQUEST_TIMEOUT", 5)
    read_timeout: int = _get_env_int("MEM0_READ_TIMEOUT", 3)
    blacklist_ttl: int = _get_env_int("MEM0_BLACKLIST_TTL", 900)
    queue_max_size: int = _get_env_int("MEM0_QUEUE_MAX_SIZE", 1000)

    @property
    def mem0_config(self) -> dict:
        """
        构造 mem0ai 客户端初始化所需的配置字典。
        使用火山引擎 Mem0 托管服务，对应 mem0ai 0.1.118 的配置规范。
        """
        return {
            "vector_store": {
                "provider": "volcengine",
                "config": {
                    "host": self.volc_mem0_host,
                    "api_key": self.volc_mem0_api_key,
                },
            },
            # 火山引擎 Mem0 托管版默认内置 LLM/Embedder，无需额外配置
            "version": "v1.1",
        }

    def validate(self) -> list[str]:
        """校验关键配置，返回错误信息列表 (空列表表示通过)。"""
        errors: list[str] = []
        if not self.volc_mem0_api_key:
            errors.append("VOLC_MEM0_API_KEY 未配置，请在 .env 中填入火山引擎 Mem0 API Key")
        if not self.volc_mem0_host:
            errors.append("VOLC_MEM0_HOST 未配置")
        if self.qps_limit > 20:
            errors.append(f"MEM0_QPS_LIMIT={self.qps_limit} 超过火山引擎硬限 20 QPS，将触发限流")
        return errors


# 全局配置单例
CONFIG = Config()
