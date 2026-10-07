"""后端激活补救 —— 解决 QwenPaw 2.x 的插件加载时序问题。

问题
----
实测 QwenPaw 2.1.0 的启动顺序：

    Agent 工作区启动  →  插件加载 / register_memory_backend

而 ``register_memory_backend`` 的 docstring 要求 "before workspaces start"。
顺序由 QwenPaw 决定，插件无法介入，因此 Agent 首次启动时一定回退到 remelight：

    WARNING | Configured memory backend 'mem0' is unavailable for agent 'default';
              using 'remelight' until the configured backend is available

（``PluginManifest`` 也无可提前声明后端的字段；修改 agent.json 亦不触发工作区重载。
两者均已实测排除。）

解法
----
注册完成后对目标 Agent 依次执行：

  步骤 1  ``memory_registry.reserve_selection(backend_id, agent_id)``
          官方接口，docstring: "Reserve a registered backend through durable
          reload handoff." —— 登记"预留"，使配置在随后的工作区重载中被采纳。
          注意：单独调用**不会**创建实例，仅登记。
  步骤 2  ``MultiAgentManager.reload_agent(agent_id)``
          docstring: "Reload a specific agent instance with zero-downtime."
          —— 真正触发工作区重建，此时预留生效、后端被实例化。
  步骤 3  复核：确认 ``Mem0MemoryManager`` 实例真实存在

关键实现要点（均由实测踩坑得出）
--------------------------------
1. **实例判定必须用 isinstance/类属性，不能用实例 hasattr**
   ``APIRemovedInV1Proxy`` 等对象实现了万能 ``__getattr__``，实例级
   ``hasattr`` 恒为真，会产生大量假阳性（实测 827040 个对象中 16 个假阳性）。
2. **GC 扫描不能按类名匹配**
   实测按 ``type(obj).__name__ == "MultiAgentManager"`` 命中数为 0，
   改用 ``isinstance(obj, MultiAgentManager)`` 才可靠（同时天然排除类对象）。
3. **成功判据不能只看 ``active_agent_ids``**
   该接口会把"预留"也计为活跃，实测出现"报告已激活但实例并不存在"的假成功。
   必须以是否存在真实实例为准。

安全：全部异常被捕获；不阻塞启动；幂等。
"""
from __future__ import annotations

import asyncio
import datetime
import gc
import inspect
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger("qwenpaw_mem0")

PLUGIN_ID = "mem0-integration"
BACKEND_ID = "mem0"
_WORKSPACES = Path.home() / ".qwenpaw" / "workspaces"

# 首次尝试前的延迟（实测 "Background startup completed in 24.8s"）
_STARTUP_DELAY = 20.0
# 重试窗口
_RETRY_ATTEMPTS = 6
_RETRY_INTERVAL = 10.0


def _log(msg: str) -> None:
    logger.info(msg)
    try:
        path = Path(__file__).resolve().parent / "backend.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} [activate] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 读取各 Agent 的配置
# ---------------------------------------------------------------------------
def _iter_agent_ids() -> List[str]:
    try:
        return sorted(
            p.name for p in _WORKSPACES.iterdir()
            if p.is_dir() and (p / "agent.json").exists()
        )
    except OSError:
        return []


def _configured_backend(agent_id: str) -> str:
    try:
        with open(_WORKSPACES / agent_id / "agent.json", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return ""

    def find(obj: Any, depth: int = 0) -> str:
        if depth > 4 or not isinstance(obj, dict):
            return ""
        if "memory_manager_backend" in obj:
            return str(obj["memory_manager_backend"] or "")
        for v in obj.values():
            found = find(v, depth + 1)
            if found:
                return found
        return ""

    return find(cfg)


# ---------------------------------------------------------------------------
# 实例判定（不用实例级 hasattr，避免万能 __getattr__ 假阳性）
# ---------------------------------------------------------------------------
_MANAGER_CLS: Any = None
_MANAGER_CLS_TRIED = False

_BACKEND_CLS: Any = None


def _manager_class() -> Any:
    global _MANAGER_CLS, _MANAGER_CLS_TRIED
    if not _MANAGER_CLS_TRIED:
        _MANAGER_CLS_TRIED = True
        try:
            from qwenpaw.app.multi_agent_manager import MultiAgentManager
            _MANAGER_CLS = MultiAgentManager
        except Exception as exc:
            _log(f"  MultiAgentManager 类导入失败: {type(exc).__name__}: {exc}")
            _MANAGER_CLS = None
    return _MANAGER_CLS


def _is_manager(obj: Any) -> bool:
    """是否为 MultiAgentManager 实例（优先 isinstance，最可靠）。"""
    if obj is None or isinstance(obj, type):
        return False
    cls = _manager_class()
    if cls is not None:
        try:
            return isinstance(obj, cls)
        except Exception:
            pass
    # 兜底：检查"类型上"的属性（而非实例上），可规避实例级万能 __getattr__
    t = type(obj)
    return hasattr(t, "reload_agent") and hasattr(t, "preload_agent")


def _mem0_backend_class() -> Any:
    global _BACKEND_CLS
    if _BACKEND_CLS is None:
        try:
            from mem0_service.memory_backend import Mem0MemoryManager
            _BACKEND_CLS = Mem0MemoryManager
        except Exception:
            _BACKEND_CLS = False
    return _BACKEND_CLS or None


def _live_mem0_instances() -> List[Any]:
    """GC 中真实存在的 Mem0MemoryManager 实例（权威成功判据）。"""
    cls = _mem0_backend_class()
    if cls is None:
        return []
    found: List[Any] = []
    try:
        for obj in gc.get_objects():
            try:
                if isinstance(obj, cls):
                    found.append(obj)
            except Exception:
                continue
    except Exception as exc:
        _log(f"  Mem0 实例扫描失败: {type(exc).__name__}: {exc}")
    return found


def _mem0_instance_agents() -> List[str]:
    """返回已有真实 Mem0 实例的 agent_id 列表。"""
    agents = []
    for inst in _live_mem0_instances():
        agent_id = getattr(inst, "agent_id", None) or getattr(inst, "_agent_id", None)
        agents.append(str(agent_id) if agent_id else "?")
    return sorted(set(agents))


# ---------------------------------------------------------------------------
# MultiAgentManager 实例定位（多策略）
# ---------------------------------------------------------------------------
def _find_via_module_globals() -> Optional[Any]:
    """策略 1：qwenpaw.app.* 模块全局对象。"""
    for mod_name in ("qwenpaw.app._app", "qwenpaw.app.multi_agent_manager"):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for name, obj in list(vars(mod).items()):
            if name.startswith("__"):
                continue
            if _is_manager(obj):
                _log(f"  [globals] ★ {mod_name}.{name}")
                return obj
    return None


def _find_via_app_state() -> Optional[Any]:
    """策略 2：FastAPI app.state 及路由处理器的宿主对象。"""
    try:
        mod = __import__("qwenpaw.app._app", fromlist=["*"])
        app = getattr(mod, "app", None)
    except Exception as exc:
        _log(f"  [app] 导入失败: {type(exc).__name__}")
        return None
    if app is None:
        _log("  [app] qwenpaw.app._app.app 不存在")
        return None

    # 2a) app.state
    try:
        state = getattr(app, "state", None)
        if state is not None:
            names = [n for n in dir(state) if not n.startswith("__")]
            _log(f"  [app] state 成员: {names[:30]}")
            for n in names:
                try:
                    obj = getattr(state, n)
                except Exception:
                    continue
                if _is_manager(obj):
                    _log(f"  [app] ★ app.state.{n}")
                    return obj
    except Exception as exc:
        _log(f"  [app] state 探测失败: {type(exc).__name__}")

    # 2b) 路由端点的宿主对象（bound method 的 __self__）
    try:
        routes = list(getattr(app, "routes", []))
        _log(f"  [app] 路由数: {len(routes)}")
        owners: List[str] = []
        for route in routes:
            ep = getattr(route, "endpoint", None)
            owner = getattr(ep, "__self__", None)
            if owner is None:
                continue
            owners.append(type(owner).__name__)
            if _is_manager(owner):
                _log(f"  [app] ★ 路由宿主: {type(owner).__name__}")
                return owner
            # 宿主对象的一层属性
            for attr in dir(owner):
                if attr.startswith("__"):
                    continue
                try:
                    inner = getattr(owner, attr)
                except Exception:
                    continue
                if _is_manager(inner):
                    _log(f"  [app] ★ 路由宿主 {type(owner).__name__}.{attr}")
                    return inner
        _log(f"  [app] 路由宿主类型: {sorted(set(owners))[:20]}")
    except Exception as exc:
        _log(f"  [app] 路由探测失败: {type(exc).__name__}")

    return None


def _find_via_gc() -> Optional[Any]:
    """策略 3：GC 扫描（isinstance 判定，同时按模块名兜底）。"""
    cls = _manager_class()
    try:
        all_objs = gc.get_objects()
    except Exception as exc:
        _log(f"  [gc] get_objects 失败: {exc}")
        return None

    hits: List[Any] = []
    module_hits: List[str] = []
    for obj in all_objs:
        try:
            if _is_manager(obj):
                hits.append(obj)
                continue
            # 兜底：按定义模块名匹配（应对类名被改写的情况）
            t = type(obj)
            mod = getattr(t, "__module__", "") or ""
            if "multi_agent_manager" in mod and not isinstance(obj, type):
                module_hits.append(f"{mod}.{t.__name__}")
                hits.append(obj)
        except Exception:
            continue

    _log(f"  [gc] 扫描 {len(all_objs)} 个对象；isinstance 命中 {len(hits)} 个")
    if module_hits:
        _log(f"  [gc] 模块名匹配: {sorted(set(module_hits))[:10]}")
    if cls is None:
        _log("  [gc] 注意：MultiAgentManager 类不可用，已退化为接口判定")
    if hits:
        _log(f"  [gc] ★ 候选: {[type(x).__name__ for x in hits]}")
        return hits[0]
    return None


def _find_manager(diagnose: bool = True) -> Optional[Any]:
    for finder, label in (
        (_find_via_module_globals, "globals"),
        (_find_via_app_state, "app"),
        (_find_via_gc, "gc"),
    ):
        try:
            mgr = finder()
        except Exception as exc:
            _log(f"  [{label}] 查找异常: {type(exc).__name__}: {exc}")
            continue
        if mgr is not None:
            return mgr
    return None


# ---------------------------------------------------------------------------
# 步骤 1：reserve_selection
# ---------------------------------------------------------------------------
def _reserve(agent_ids: List[str]) -> None:
    try:
        from qwenpaw.agents.memory.reme_light_memory_manager import memory_registry as reg
    except Exception as exc:
        _log(f"  [reserve] registry 导入失败: {type(exc).__name__}")
        return
    fn = getattr(reg, "reserve_selection", None)
    if fn is None:
        _log("  [reserve] registry 无 reserve_selection")
        return
    for agent_id in agent_ids:
        try:
            lease = fn(BACKEND_ID, agent_id)
            _log(f"  [reserve] reserve_selection({BACKEND_ID}, {agent_id}) -> {type(lease).__name__}")
        except Exception as exc:
            _log(f"  [reserve] {agent_id} 失败: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 步骤 2：reload_agent
# ---------------------------------------------------------------------------
def _run_awaitable(result: Any, loop: Any, timeout: float = 60.0) -> Any:
    """若结果是协程，则在应用事件循环中执行它并等结果。

    实测 ``MultiAgentManager.reload_agent`` 是**协程函数**（虽然签名注解写的是
    ``-> bool``），同步调用只返回 coroutine 而不执行。必须在应用的事件循环里
    await 才真正生效。
    """
    if not inspect.isawaitable(result):
        return result
    if loop is not None and not loop.is_closed():
        try:
            fut = asyncio.run_coroutine_threadsafe(result, loop)
            return fut.result(timeout=timeout)
        except Exception as exc:
            _log(f"  [reload] 在应用事件循环中执行失败: {type(exc).__name__}: {exc}")
            return None
    # 兜底：当前无可用循环时自建一个
    try:
        return asyncio.run(result)
    except Exception as exc:
        _log(f"  [reload] 自建事件循环执行失败: {type(exc).__name__}: {exc}")
        return None


def _reload(agent_ids: List[str], loop: Any = None) -> None:
    mgr = _find_manager()
    if mgr is None:
        _log("  [reload] ⚠️ 未能定位 MultiAgentManager 实例")
        return
    _log(f"  [reload] 使用实例: {type(mgr).__name__} @ {id(mgr)}")
    for agent_id in agent_ids:
        try:
            raw = mgr.reload_agent(agent_id)
            kind = "coroutine" if inspect.isawaitable(raw) else "value"
            _log(f"  [reload] reload_agent({agent_id}) 返回 {kind}")
            result = _run_awaitable(raw, loop)
            _log(f"  [reload] reload_agent({agent_id}) -> {result}")
        except Exception as exc:
            _log(f"  [reload] reload_agent({agent_id}) 失败: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def activate_configured_agents(
    runtime: Any = None,
    loop: Any = None,
    delay: float = _STARTUP_DELAY,
) -> None:
    """对配置为 mem0 但未真正激活的 Agent 执行激活（幂等、多路径、带重试）。

    Args:
        runtime: ``api.runtime``（可选，用于定位 manager）。
        loop: 应用的事件循环。``reload_agent`` 是协程函数，必须在此循环中 await。
    """
    if delay > 0:
        time.sleep(delay)

    wanted = [a for a in _iter_agent_ids() if _configured_backend(a) == BACKEND_ID]
    if not wanted:
        _log(f"没有 Agent 配置为 {BACKEND_ID}，无需激活")
        return
    _log(f"目标 Agent（配置为 {BACKEND_ID}）: {wanted}")
    _log(f"事件循环: {'可用' if loop is not None else '不可用（将用自建循环兜底）'}")

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        live = _mem0_instance_agents()
        need = [a for a in wanted if a not in live]
        _log(f"[第 {attempt}/{_RETRY_ATTEMPTS} 次] 真实实例: {live}；待激活: {need}")
        if not need:
            _log(f"✓ 全部目标 Agent 已激活 Mem0（实例: {live}）")
            return

        _reserve(need)
        _reload(need, loop)

        live_after = _mem0_instance_agents()
        if all(a in live_after for a in need):
            _log(f"✓ 激活成功，真实实例: {live_after}")
            return
        if live_after:
            _log(f"  部分激活: {live_after}")

        if attempt < _RETRY_ATTEMPTS:
            time.sleep(_RETRY_INTERVAL)

    _log(
        f"⚠️ 重试 {_RETRY_ATTEMPTS} 次后仍未激活（真实实例: {_mem0_instance_agents()}）。"
        f"预留已登记，可尝试在 QwenPaw 界面中重载该 Agent，或反馈此诊断日志。"
    )


def spawn_activation_thread(
    runtime: Any = None,
    loop: Any = None,
    delay: float = _STARTUP_DELAY,
) -> None:
    """在后台线程中执行激活，绝不阻塞或影响 QwenPaw 启动。"""
    def _run() -> None:
        try:
            activate_configured_agents(runtime=runtime, loop=loop, delay=delay)
        except Exception as exc:
            _log(f"激活流程异常: {type(exc).__name__}: {exc}")
            import traceback
            _log(f"traceback: {traceback.format_exc()}")

    thread = threading.Thread(target=_run, name="mem0-backend-activate", daemon=True)
    thread.start()
    _log(f"激活线程已启动（延迟 {delay}s，最多重试 {_RETRY_ATTEMPTS} 次）")