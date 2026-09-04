"""Mem0 Integration Plugin — Hook 兜底 + Skill 增强双方案 (自包含版)。

通过 QwenPaw 插件系统注入火山引擎 Mem0 记忆层：
1. **Hook 兜底** (register_class_hook): pre_reply 自动检索注入, post_reply 异步写入
2. **Skill 增强** (包装 _register_hooks): 5 个记忆管理工具注册到 Toolkit
3. **备份同步**: 将插件目录同步到 default workspace 的 _mem0_backup/, 让 QwenPaw 备份自动覆盖
4. **恢复同步**: 检测 workspace 内的 _mem0_backup/, 恢复到插件目录

设计要点：
- 自包含: mem0_service 代码内联在插件目录, 不依赖任何外部固定目录
- 跨升级: 插件目录在用户级路径 ~/.qwenpaw/plugins/, 不被升级覆盖
- 备份友好: 通过 workspace 同步让 QwenPaw 备份自动覆盖 mem0 部署
- 所有异常被捕获, 不影响 QwenPaw 正常运行
"""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("qwenpaw.plugins.mem0_integration")

# 插件目录 & 日志路径
_PLUGIN_DIR = Path(__file__).parent.resolve()
_MEM0_SERVICE_DIR = _PLUGIN_DIR / "mem0_service"
_LOG_FILE = _PLUGIN_DIR / "plugin.log"

# workspace 内的备份同步目录名
_BACKUP_SYNC_DIRNAME = "_mem0_backup"

# default workspace 路径
_DEFAULT_WORKSPACE = Path.home() / ".qwenpaw" / "workspaces" / "default"


def _log(msg: str) -> None:
    """写文件日志, 不被 QwenPaw logging 重配置影响。"""
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


# 将插件内的 mem0_service 目录加入 sys.path
if str(_MEM0_SERVICE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_MEM0_SERVICE_DIR.parent))


class Mem0IntegrationPlugin:
    """Mem0 集成插件主类 (自包含 + 备份同步)。"""

    def __init__(self):
        self.plugin_id = "mem0-integration"
        self._injected = False
        self._inject_lock = threading.Lock()

    def register(self, api) -> None:
        """插件注册入口 (由 PluginLoader 调用)。"""
        _log(f"[plugin] register() 被调用, plugin_id={api.plugin_id}")

        # 1. 恢复同步 (priority=5, 最先执行): 从 workspace 恢复 mem0 文件
        api.register_startup_hook(
            hook_name="mem0_restore_from_workspace",
            callback=self._restore_from_workspace,
            priority=5,
        )
        _log("[plugin] restore_from_workspace hook 已注册 (priority=5)")

        # 2. 备份同步 (priority=8): 将插件目录同步到 workspace
        api.register_startup_hook(
            hook_name="mem0_backup_sync",
            callback=self._backup_sync,
            priority=8,
        )
        _log("[plugin] backup_sync hook 已注册 (priority=8)")

        # 3. 注入 Hook + Skill (priority=10): 注入 Mem0 双方案
        api.register_startup_hook(
            hook_name="mem0_inject_hooks",
            callback=self._startup_inject,
            priority=10,
        )
        _log("[plugin] startup hook 已注册 (priority=10)")

        # 4. shutdown hook: 关闭前再次同步
        api.register_shutdown_hook(
            hook_name="mem0_shutdown_sync",
            callback=self._shutdown_sync,
            priority=5,
        )
        _log("[plugin] shutdown hook 已注册")

    # ================================================================
    # 备份同步: 插件目录 → workspace/_mem0_backup/
    # ================================================================

    def _backup_sync(self) -> None:
        """将插件目录的关键文件同步到 default workspace 的 _mem0_backup/。

        这样 QwenPaw 备份 default workspace 时会自动包含 mem0 部署文件。
        """
        try:
            if not _DEFAULT_WORKSPACE.exists():
                _log(f"[backup_sync] default workspace 不存在: {_DEFAULT_WORKSPACE}")
                return

            sync_dir = _DEFAULT_WORKSPACE / _BACKUP_SYNC_DIRNAME
            _log(f"[backup_sync] 同步插件目录 → {sync_dir}")

            # 先清空旧的同步目录 (排除 workspace 自己的其他文件)
            if sync_dir.exists():
                shutil.rmtree(sync_dir, ignore_errors=True)
            sync_dir.mkdir(parents=True, exist_ok=True)

            # 复制插件目录 (排除 plugin.log 和 __pycache__)
            exclude_patterns = {"plugin.log", "__pycache__", ".pyc", "test_plugin.py", "test_selfcontained.py", "test_four_hooks.py", "injection.log", ".tmp", ".bak"}
            copied = 0
            for item in _PLUGIN_DIR.iterdir():
                if item.name in exclude_patterns:
                    continue
                dst = sync_dir / item.name
                if item.is_dir():
                    # 复制目录, 排除 __pycache__
                    shutil.copytree(
                        item, dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                else:
                    shutil.copy2(item, dst)
                copied += 1

            # 写入恢复元信息
            meta_file = sync_dir / "_RESTORE_INFO.json"
            import json
            meta = {
                "plugin_id": self.plugin_id,
                "version": "1.0.0",
                "synced_at": datetime.datetime.now().isoformat(),
                "source": str(_PLUGIN_DIR),
                "files_copied": copied,
                "description": "Mem0 部署备份 - 由 mem0-integration 插件自动同步",
            }
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            _log(f"[backup_sync] 同步完成: {copied} 项, 元信息已写入 _RESTORE_INFO.json")

        except Exception as exc:
            _log(f"[backup_sync] 同步失败: {exc}")
            import traceback
            _log(f"[backup_sync] traceback: {traceback.format_exc()}")

    # ================================================================
    # 恢复同步: workspace/_mem0_backup/ → 插件目录
    # ================================================================

    def _restore_from_workspace(self) -> None:
        """从 default workspace 的 _mem0_backup/ 恢复插件目录。

        场景: 用户从备份恢复 QwenPaw 后, workspace 内的 _mem0_backup/ 被还原,
        此函数将其内容复制回插件目录, 确保插件代码完整。
        """
        try:
            sync_dir = _DEFAULT_WORKSPACE / _BACKUP_SYNC_DIRNAME
            if not sync_dir.exists():
                _log(f"[restore] 同步目录不存在, 跳过: {sync_dir}")
                return

            # 检查恢复元信息
            meta_file = sync_dir / "_RESTORE_INFO.json"
            if not meta_file.exists():
                _log("[restore] _RESTORE_INFO.json 不存在, 跳过恢复")
                return

            import json
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)

            _log(f"[restore] 检测到备份同步目录: {sync_dir}")
            _log(f"[restore] 备份元信息: version={meta.get('version')}, synced_at={meta.get('synced_at')}")

            # 比较时间戳: 仅当 workspace 备份比插件目录新时才恢复
            # (避免每次启动都覆盖)
            plugin_log = _PLUGIN_DIR / "plugin.log"
            sync_time = meta.get("synced_at", "")
            if sync_time and plugin_log.exists():
                # 简单策略: 检查 mem0_service/.env 是否存在于插件目录
                # 如果不存在, 说明插件目录被清空, 需要恢复
                env_file = _MEM0_SERVICE_DIR / ".env"
                if env_file.exists():
                    _log("[restore] 插件目录完整, 跳过恢复")
                    return

            _log("[restore] 插件目录不完整, 开始恢复...")

            # 恢复: 从 sync_dir 复制回 _PLUGIN_DIR
            exclude_patterns = {"plugin.log", "__pycache__", ".pyc", "_RESTORE_INFO.json", "test_plugin.py", "test_selfcontained.py", "test_four_hooks.py", "injection.log", ".tmp", ".bak"}
            restored = 0
            for item in sync_dir.iterdir():
                if item.name in exclude_patterns:
                    continue
                dst = _PLUGIN_DIR / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(
                        item, dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                else:
                    shutil.copy2(item, dst)
                restored += 1

            _log(f"[restore] 恢复完成: {restored} 项已复制回插件目录")

        except Exception as exc:
            _log(f"[restore] 恢复失败: {exc}")
            import traceback
            _log(f"[restore] traceback: {traceback.format_exc()}")

    # ================================================================
    # shutdown 同步: 关闭前再次同步, 确保最新状态被备份
    # ================================================================

    async def _shutdown_sync(self) -> None:
        """关闭前同步, 确保最新的插件状态被写入 workspace 备份目录。"""
        _log("[shutdown] 执行关闭前同步")
        self._backup_sync()
        _log("[shutdown] 关闭前同步完成")

    # ================================================================
    # 注入 Hook + Skill (核心逻辑)
    # ================================================================

    async def _startup_inject(self) -> None:
        """启动钩子: 注入 Hook 兜底 + Skill 增强。

        处理 QwenPawAgent 类加载时序: 类可能在 _load_plugins() 之后才被加载,
        所以启动一个后台线程持续重试, 直到类加载完成或超时。
        """
        _log("[startup] _startup_inject 开始执行")

        # 先尝试同步注入
        if self._try_inject():
            _log("[startup] 同步注入成功")
            return

        # 同步注入失败 (QwenPawAgent 未加载), 启动后台重试线程
        _log("[startup] QwenPawAgent 未加载, 启动后台重试线程")
        thread = threading.Thread(
            target=self._retry_inject_background,
            name="mem0-inject-retry",
            daemon=True,
        )
        thread.start()

    def _retry_inject_background(self) -> None:
        """后台线程: 持续重试注入, 直到成功或超时 (60 秒)。"""
        deadline = time.time() + 60.0
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            time.sleep(1.0)
            _log(f"[retry] 第 {attempt} 次重试注入...")
            if self._try_inject():
                _log(f"[retry] 第 {attempt} 次重试成功")
                return
        _log(f"[retry] 重试超时 (60s), 共 {attempt} 次尝试, 注入失败")

    def _try_inject(self) -> bool:
        """尝试注入 Hook + Skill, 返回是否成功。

        所有异常被捕获, 不影响 QwenPaw 正常运行。
        """
        with self._inject_lock:
            if self._injected:
                return True

            try:
                # === 升级兼容性检查 ===
                # 1. mem0ai 版本检查 (锁定 0.1.118, 升级后可能不兼容)
                try:
                    import mem0
                    mem0_version = getattr(mem0, "__version__", "unknown")
                    _log(f"[inject] mem0ai 版本: {mem0_version}")
                    if mem0_version != "0.1.118":
                        _log(f"[inject] ⚠️ mem0ai 版本 {mem0_version} != 0.1.118, 可能不兼容")
                        # 不阻止注入, 但记录警告 (向后兼容尝试)
                except ImportError as exc:
                    _log(f"[inject] ⚠️ mem0ai 未安装: {exc}")
                    return True  # 不重试, 缺少依赖

                # 2. agentscope API 兼容性检查
                try:
                    from agentscope.agent._agent_base import AgentBase
                    assert hasattr(AgentBase, "register_class_hook"), "register_class_hook 不存在"
                    _log("[inject] agentscope API 兼容性检查通过")
                except (ImportError, AssertionError) as exc:
                    _log(f"[inject] ⚠️ agentscope API 不兼容: {exc}")
                    return True  # 不重试, API 已变更

                # 3. _MemoryMark.HINT 机制检查 (用于 Hint 注入)
                try:
                    from agentscope.agent._react_agent import _MemoryMark
                    assert hasattr(_MemoryMark, "HINT"), "_MemoryMark.HINT 不存在"
                    _log("[inject] _MemoryMark.HINT 机制检查通过")
                except (ImportError, AssertionError) as exc:
                    _log(f"[inject] ⚠️ _MemoryMark.HINT 机制不兼容: {exc}, 将使用 sys_prompt 回退")

                # 验证配置 (从插件内的 mem0_service 加载)
                from mem0_service.config import CONFIG
                errors = CONFIG.validate()
                if errors:
                    for e in errors:
                        _log(f"[inject] 配置错误: {e}")
                    _log("[inject] 配置不完整, 跳过注入")
                    return True  # 不重试, 配置问题需要用户修复

                # 导入 QwenPawAgent 类
                try:
                    from qwenpaw.agents.react_agent import QwenPawAgent
                except ImportError as exc:
                    _log(f"[inject] QwenPawAgent 未加载: {exc}")
                    return False  # 需要重试

                # 导入 Mem0 组件 (从插件内的 mem0_service 加载)
                from mem0_service.hooks.memory_hooks import MemoryHooks
                from mem0_service.skills.memory_skills import ALL_TOOLS
                _log("[inject] 模块导入成功 (MemoryHooks + ALL_TOOLS)")

                hooks = MemoryHooks()

                # --- 1. Hook 兜底层: 四钩子全注册 (类级别, 对所有 QwenPawAgent 实例自动生效) ---
                # 检索层: pre_reply (主) + pre_reasoning (兜底), 去重标志 _mem0_searched
                QwenPawAgent.register_class_hook(
                    "pre_reply",
                    "mem0_search_inject",
                    hooks.pre_reply,
                )
                QwenPawAgent.register_class_hook(
                    "pre_reasoning",
                    "mem0_search_fallback",
                    hooks.pre_reasoning,
                )
                # 写入层: post_acting (工具收集) + post_reply (主写入), 去重通过 _mem0_tool_buffer 合并
                QwenPawAgent.register_class_hook(
                    "post_acting",
                    "mem0_tool_collect",
                    hooks.post_acting,
                )
                QwenPawAgent.register_class_hook(
                    "post_reply",
                    "mem0_async_write",
                    hooks.post_reply,
                )
                _log("[inject] Hook 兜底层注入成功 (四钩子: pre_reply + pre_reasoning + post_acting + post_reply)")

                # --- 2. Skill 增强层: 包装 _register_hooks 以注入 5 个记忆工具 ---
                _original_register_hooks = QwenPawAgent._register_hooks

                def _patched_register_hooks(self):
                    _original_register_hooks(self)
                    agent_name = getattr(self, "name", "?")
                    _log(f"[inject] _patched_register_hooks 被调用, agent={agent_name}")
                    for tool_fn in ALL_TOOLS:
                        try:
                            self.toolkit.register_tool_function(
                                tool_fn,
                                namesake_strategy="skip",
                            )
                            _log(f"[inject] 工具注册成功: {tool_fn.__name__}, agent={agent_name}")
                        except Exception as exc:
                            _log(f"[inject] 工具注册失败: {tool_fn.__name__} -> {exc}")

                QwenPawAgent._register_hooks = _patched_register_hooks
                _log("[inject] Skill 增强层注入成功 (5 个工具: search/list/delete/update/add)")

                self._injected = True
                _log("[inject] === Mem0 双方案注入完成 ===")
                return True

            except Exception as exc:
                _log(f"[inject] 注入异常: {exc}")
                import traceback
                _log(f"[inject] traceback: {traceback.format_exc()}")
                return False  # 重试


# 插件实例 (PluginLoader 期望模块导出 `plugin` 对象)
plugin = Mem0IntegrationPlugin()
_log(f"[module] backend.py 已加载 (自包含版), plugin={plugin.plugin_id}")
_log(f"[module] _PLUGIN_DIR={_PLUGIN_DIR}")
_log(f"[module] _MEM0_SERVICE_DIR={_MEM0_SERVICE_DIR}")
