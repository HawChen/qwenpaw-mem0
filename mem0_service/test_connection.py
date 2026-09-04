"""验证火山引擎 Mem0 连通性测试脚本 (v2)"""
import sys
import time
from config import CONFIG

def test_connection():
    print('=== 1. 配置检查 ===')
    print(f'VOLC_MEM0_HOST = {CONFIG.volc_mem0_host}')
    print(f'VOLC_MEM0_API_KEY = {CONFIG.volc_mem0_api_key[:8]}...{CONFIG.volc_mem0_api_key[-4:]} (len={len(CONFIG.volc_mem0_api_key)})')
    errors = CONFIG.validate()
    if errors:
        print(f'[FAIL] 配置验证失败: {errors}')
        return False
    print('[OK] 配置验证通过')
    print()

    print('=== 2. 检查 mem0 SDK 版本 ===')
    try:
        import mem0
        print(f'[OK] mem0 安装路径: {mem0.__file__}')

        from mem0 import MemoryClient
        print(f'[OK] MemoryClient 导入成功')
    except ImportError as e:
        print(f'[FAIL] mem0 导入失败: {e}')
        return False
    print()

    print('=== 3. 创建 MemoryClient ===')
    try:
        import httpx
        http_client = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        client = MemoryClient(
            api_key=CONFIG.volc_mem0_api_key,
            host=CONFIG.volc_mem0_host,
            client=http_client,
        )
        print('[OK] MemoryClient 对象创建成功')
    except Exception as e:
        print(f'[FAIL] MemoryClient 创建失败: {e}')
        return False
    print()

    print('=== 4. 搜索测试 (验证连通性) ===')
    try:
        start = time.time()
        results = client.search(query='test', user_id='default_user', top_k=3)
        elapsed = time.time() - start
        print(f'[OK] 搜索请求成功 (耗时 {elapsed:.2f}s)')
        if isinstance(results, dict):
            print(f'     响应 keys: {list(results.keys())}')
            items = results.get('results', [])
            print(f'     返回 {len(items)} 条结果')
            for i, r in enumerate(items):
                if isinstance(r, dict):
                    print(f'     {i+1}. [{r.get("id", "?")}] {str(r.get("memory", str(r)))[:60]}')
                else:
                    print(f'     {i+1}. {str(r)[:60]}')
        elif isinstance(results, list):
            print(f'     返回 {len(results)} 条结果')
            for i, r in enumerate(results):
                if isinstance(r, dict):
                    print(f'     {i+1}. [{r.get("id", "?")}] {str(r.get("memory", str(r)))[:60]}')
                else:
                    print(f'     {i+1}. {str(r)[:60]}')
    except Exception as e:
        print(f'[FAIL] 搜索请求失败: {e}')
        import traceback
        traceback.print_exc()
        return False
    print()

    print('=== 5. 写入测试 (messages format) ===')
    try:
        add_result = client.add(
            messages=[{"role": "user", "content": "这是一条测试记忆，用于验证火山引擎Mem0连通性。测试时间: " + time.strftime("%Y-%m-%d %H:%M:%S")}],
            user_id='default_user',
        )
        print(f'[OK] 写入成功')
        print(f'     结果: {add_result}')
    except Exception as e:
        print(f'[FAIL] 写入失败: {e}')
        import traceback
        traceback.print_exc()
        return False
    print()

    print('=== 6. 验证新写入的记忆可被搜索到 ===')
    try:
        time.sleep(1)
        results2 = client.search(query='测试记忆 火山引擎', user_id='default_user', top_k=5)
        if isinstance(results2, dict):
            items = results2.get('results', [])
        else:
            items = results2 if isinstance(results2, list) else []
        print(f'[OK] 搜索到 {len(items)} 条相关记忆')
        for i, r in enumerate(items):
            if isinstance(r, dict):
                print(f'     {i+1}. [{r.get("id", "?")}] {str(r.get("memory", str(r)))[:60]}')
            else:
                print(f'     {i+1}. {str(r)[:60]}')
    except Exception as e:
        print(f'[WARN] 二次搜索: {e}')
    print()

    print('=== 7. 列出所有记忆 ===')
    try:
        for method_name in ['get_all']:
            method = getattr(client, method_name, None)
            if method:
                try:
                    all_memories = method(user_id='default_user')
                    if isinstance(all_memories, dict):
                        items = all_memories.get('results', [])
                    else:
                        items = all_memories if isinstance(all_memories, list) else []
                    print(f'[OK] client.{method_name}() 调用成功, 共 {len(items)} 条记忆')
                    for i, r in enumerate(items):
                        if isinstance(r, dict):
                            print(f'     {i+1}. [{r.get("id", "?")}] {str(r.get("memory", str(r)))[:60]}')
                        else:
                            print(f'     {i+1}. {str(r)[:60]}')
                    break
                except Exception as e:
                    print(f'[INFO] client.{method_name}(): {e}')
        else:
            print('[INFO] 未找到 get_all 方法，跳过')
    except Exception as e:
        print(f'[INFO] 列出手动测试: {e}')

    print()
    print('======================================')
    print('全部测试完成')
    return True


if __name__ == '__main__':
    success = test_connection()
    sys.exit(0 if success else 1)
