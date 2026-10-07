"""Mem0 Integration Plugin — QwenPaw 2.x 版（官方记忆后端）。

相较于旧版（Hook 兜底 + Skill 增强，依赖 AgentScope 类钩子）的变化：

1. **注册官方记忆后端**：通过 ``api.register_memory_backend`` 把
   ``Mem0MemoryManager`` 注册为可选后端（``backend_id="mem0"``）。
   用户在 ``agent.json`` 里将 ``memory_manager_backend`` 设为 ``"mem0"``
   即可对指定 Agent 启用；其余 Agent 继续使用本地 ReMe，互不影响。

2. **移除类钩子注入**：agentscope 2.0.7 已删除 ``register_class_hook`` /
   ``agentscope.memory`` / ``_MemoryMark``，旧注入逻辑全部失效，故删除。

3. **保留备份同步**：原需求要求 QwenPaw 备份能覆盖本插件内容，
   故保留 startup/shutdown 的 workspace 同步与恢复逻辑。

设计要点：
- 自包含：``mem0_service`` 内联在插件目录，不依赖外部路径
- 跨升级：插件目录位于用户级 ``~/.qwenpaw/plugins/``，不被升级覆盖
- 备份友好：同步到 default workspace 的 ``_mem0_backup/``
- 容错优先：所有异常被捕获，绝不影响 QwenPaw 正常运行
"""
import datetime
import json
import logging
import shutil
import sys
import threading
from pathlib import Path

logger = logging.getLogger("qwenpaw.plugins.mem0_integration")

# 插件目录 & 日志路径
_PLUGIN_DIR = Path(__file__).parent.resolve()
_MEM0_SERVICE_DIR = _PLUGIN_DIR / "mem0_service"
_LOG_FILE = _PLUGIN_DIR / "plugin.log"

# workspace 内的备份同步目录名
_BACKUP_SYNC_DIRNAME = "_mem0_backup"

# default workspace 路径
_DEFAULT_WORKSPACE = Path.home() / ".qwenpaw" / "workspaces" / "default"

# 备份/恢复时排除的文件（本地状态，不应跨机覆盖）
_EXCLUDE = {
    "plugin.log",
    "backend.log",
    "injection.log",
    "usage.json",
    "usage.json.tmp",
    "__pycache__",
    ".pyc",
    "_RESTORE_INFO.json",
    ".tmp",
    ".bak",
}

# 将插件目录加入 sys.path，使 mem0_service 可作为包导入
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _log(msg: str) -> None:
    """写文件日志，不被 QwenPaw logging 重配置影响。"""
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class Mem0IntegrationPlugin:
    """Mem0 集成插件主类（官方记忆后端 + 备份同步）。"""

    plugin_id = "mem0-integration"

    def __init__(self) -> None:
        self._backend_registered = False
        self._runtime = None
        self._lock = threading.Lock()

    # ==================================================================
    # 注册入口
    # ==================================================================
    def register(self, api) -> None:
        """插件注册入口（由 PluginLoader 调用）。"""
        self._runtime = getattr(api, "runtime", None)
        _log(f"[plugin] register() 被调用, plugin_id={api.plugin_id}")

        # 1. 注册官方记忆后端 —— 必须在工作区启动前完成，故直接调用
        self._register_memory_backend(api)

        # 2. 恢复同步（priority=5，最先执行）
        api.register_startup_hook(
            hook_name="mem0_restore_from_workspace",
            callback=self._restore_from_workspace,
            priority=5,
        )

        # 3. 备份同步（priority=8）
        api.register_startup_hook(
            hook_name="mem0_backup_sync",
            callback=self._backup_sync,
            priority=8,
        )

        # 4. shutdown 钩子：关闭前再次同步
        api.register_shutdown_hook(
            hook_name="mem0_shutdown_sync",
            callback=self._shutdown_sync,
            priority=5,
        )

        # 5. 后端激活补救（priority=90，靠后执行）
        #    QwenPaw 2.x 的 Agent 工作区先于插件加载启动，导致后端注册"晚了一步"，
        #    配置为 mem0 的 Agent 会先回退到 remelight。此钩子在注册完成后
        #    触发一次零停机重载，让工作区重新解析记忆后端。
        api.register_startup_hook(
            hook_name="mem0_activate_backend",
            callback=self._activate_backend,
            priority=90,
        )
        _log("[plugin] 全部钩子注册完成（restore / backup / shutdown / activate）")

    # ==================================================================
    # 官方记忆后端注册（核心）
    # ==================================================================
    def _register_memory_backend(self, api) -> None:
        """把 Mem0MemoryManager 注册为可选记忆后端。"""
        with self._lock:
            if self._backend_registered:
                return
            try:
                from mem0_service.memory_backend import BACKEND_ID, Mem0MemoryManager

                api.register_memory_backend(
                    backend_id=BACKEND_ID,
                    factory=Mem0MemoryManager,
                    label="火山引擎 Mem0",
                    metadata={
                        "description": "云端托管记忆，按 Agent 可选；计费自 2026-11-02 起",
                        "plugin": self.plugin_id,
                        "version": "2.0.0",
                    },
                )
                self._backend_registered = True
                _log(f"[plugin] ✓ 记忆后端注册成功: backend_id={BACKEND_ID}")
            except Exception as exc:  # noqa: BLE001
                _log(f"[plugin] ✗ 记忆后端注册失败: {type(exc).__name__}: {exc}")
                import traceback

                _log(f"[plugin] traceback: {traceback.format_exc()}")

    # ==================================================================
    # 后端激活补救（解决 QwenPaw 插件加载晚于 Agent 启动的时序问题）
    # ==================================================================
    async def _activate_backend(self) -> None:
        """在后台线程中对配置为 mem0 的 Agent 执行零停机重载。

        关键：``MultiAgentManager.reload_agent`` 是**协程函数**（尽管签名注解
        写的是 ``-> bool``），必须在应用的事件循环中 await 才真正执行。
        因此这里捕获当前运行的循环并传给激活线程。

        幂等：已有存活 Mem0 实例的 Agent 会被跳过。
        所有异常被捕获，绝不影响 QwenPaw 启动。
        """
        try:
            import asyncio

            from mem0_service.activation import spawn_activation_thread

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
                _log("[plugin] ⚠️ 未取到事件循环，激活将走自建循环兜底")

            runtime = getattr(self, "_runtime", None)
            spawn_activation_thread(runtime=runtime, loop=loop)
        except Exception as exc:  # noqa: BLE001
            _log(f"[plugin] 激活补救启动失败: {type(exc).__name__}: {exc}")

    # ==================================================================
    # 备份同步：插件目录 → workspace/_mem0_backup/
    # ==================================================================
    def _backup_sync(self) -> None:
        """把插件内容同步到 default workspace，使 QwenPaw 备份自动覆盖。"""
        try:
            if not _DEFAULT_WORKSPACE.exists():
                _log(f"[backup_sync] default workspace 不存在，跳过: {_DEFAULT_WORKSPACE}")
                return

            sync_dir = _DEFAULT_WORKSPACE / _BACKUP_SYNC_DIRNAME
            if sync_dir.exists():
                shutil.rmtree(sync_dir, ignore_errors=True)
            sync_dir.mkdir(parents=True, exist_ok=True)

            copied = 0
            for item in _PLUGIN_DIR.iterdir():
                if item.name in _EXCLUDE:
                    continue
                dst = sync_dir / item.name
                if item.is_dir():
                    shutil.copytree(
                        item, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
                else:
                    shutil.copy2(item, dst)
                copied += 1

            meta = {
                "plugin_id": self.plugin_id,
                "version": "2.0.0",
                "synced_at": datetime.datetime.now().isoformat(),
                "source": str(_PLUGIN_DIR),
                "files_copied": copied,
                "description": "Mem0 部署备份 - 由 mem0-integration 插件自动同步",
            }
            with open(sync_dir / "_RESTORE_INFO.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            _log(f"[backup_sync] 同步完成: {copied} 项 → {sync_dir}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[backup_sync] 同步失败: {exc}")

    # ==================================================================
    # 恢复同步：workspace/_mem0_backup/ → 插件目录
    # ==================================================================
    def _restore_from_workspace(self) -> None:
        """从备份还原插件（换机/重装后自动恢复代码与配置）。"""
        try:
            sync_dir = _DEFAULT_WORKSPACE / _BACKUP_SYNC_DIRNAME
            if not sync_dir.exists():
                _log(f"[restore] 备份目录不存在，跳过: {sync_dir}")
                return

            env_file = _MEM0_SERVICE_DIR / ".env"
            if env_file.exists():
                _log("[restore] 插件目录完整，跳过恢复")
                return

            _log("[restore] 插件目录不完整，开始恢复...")
            restored = 0
            for item in sync_dir.iterdir():
                if item.name in _EXCLUDE:
                    continue
                dst = _PLUGIN_DIR / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(
                        item, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
                else:
                    shutil.copy2(item, dst)
                restored += 1
            _log(f"[restore] 恢复完成: {restored} 项")
        except Exception as exc:  # noqa: BLE001
            _log(f"[restore] 恢复失败: {exc}")

    # ==================================================================
    async def _shutdown_sync(self) -> None:
        """关闭前同步，确保最新状态进入 workspace 备份目录。"""
        try:
            self._backup_sync()
            _log("[shutdown] 关闭前同步完成")
        except Exception as exc:  # noqa: BLE001
            _log(f"[shutdown] 同步失败: {exc}")


# 插件实例（PluginLoader 期望模块导出 plugin 对象）
plugin = Mem0IntegrationPlugin()
_log(f"[module] backend.py 已加载 (QwenPaw 2.x 版), plugin={plugin.plugin_id}")
_log(f"[module] _PLUGIN_DIR={_PLUGIN_DIR}")