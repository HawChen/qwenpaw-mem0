"""直接测试 mem0-integration 插件的注入逻辑。

运行方式::
    python test_plugin.py   # 使用已安装依赖的 Python 解释器（QwenPaw 宿主自带或自建虚拟环境）
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import os

# 加载 backend.py
plugin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend.py")  # 相对定位，避免硬编码本机路径
spec = importlib.util.spec_from_file_location("plugin_mem0_integration", plugin_path)
module = importlib.util.module_from_spec(spec)
sys.modules["plugin_mem0_integration"] = module
spec.loader.exec_module(module)

plugin_obj = module.plugin
print(f"插件加载成功: {plugin_obj.plugin_id}")
print(f"日志文件: {module._LOG_FILE}")

# 直接调用 _startup_inject (跳过 register)
print("\n--- 调用 _startup_inject() ---")
asyncio.run(plugin_obj._startup_inject())
print(f"_startup_inject() 完成, 注入状态: {plugin_obj._injected}")

# 验证 QwenPawAgent 是否被注入
print("\n--- 验证注入结果 ---")
try:
    from qwenpaw.agents.react_agent import QwenPawAgent
    method_name = QwenPawAgent._register_hooks.__name__
    print(f"QwenPawAgent._register_hooks 方法名: {method_name}")
    print(f"是否被包装: {method_name == '_patched_register_hooks'}")

    # 检查类级别钩子
    class_hooks = getattr(QwenPawAgent, "_class_pre_reply_hooks", {})
    print(f"_class_pre_reply_hooks: {list(class_hooks.keys()) if class_hooks else 'empty'}")
    class_post_hooks = getattr(QwenPawAgent, "_class_post_reply_hooks", {})
    print(f"_class_post_reply_hooks: {list(class_post_hooks.keys()) if class_post_hooks else 'empty'}")
except Exception as exc:
    print(f"验证失败: {exc}")
    import traceback
    traceback.print_exc()

# 检查日志文件
print("\n--- 插件日志 ---")
log_file = module._LOG_FILE
if os.path.exists(log_file):
    with open(log_file, "r", encoding="utf-8") as f:
        content = f.read()
    # 只打印最后 30 行
    lines = content.strip().split("\n")
    for line in lines[-30:]:
        print(f"  {line}")
else:
    print("  日志文件不存在")
