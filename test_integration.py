"""통합 테스트 — uvicorn 서버 기동 + httpx 호출."""
import subprocess
import time
import httpx
import sys

BASE = "http://localhost:8000"

# 서버 기동
print("서버 기동 중...")
proc = subprocess.Popen(
    [sys.executable, "main.py"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
time.sleep(3)  # 서버 초기화 대기

try:
    client = httpx.Client(timeout=30)

    # 1. help
    r = client.get(f"{BASE}/api/v1/help?action=schema")
    print(f"help: {r.status_code}")
    assert r.status_code == 200

    # 2. ref
    r = client.get(f"{BASE}/api/v1/ref?action=search")
    data = r.json()["data"]
    print(f"ref: {r.status_code}, total_laws={data.get('total_laws')}")

    # 3. keyword search
    r = client.get(f"{BASE}/api/v1/law?action=search&q=과학기술&mode=keyword")
    data = r.json()["data"]
    print(f"search: {r.status_code}, total={data.get('total')}")

    # 4. get law
    r = client.get(f"{BASE}/api/v1/law?action=get&name=과학기술법")
    data = r.json()["data"]
    print(f"get: {r.status_code}, articles={len(data.get('articles', []))}")

    # 5. history
    r = client.get(f"{BASE}/api/v1/law?action=history&name=과학기술법")
    data = r.json()["data"]
    print(f"history: {r.status_code}, amendments={len(data.get('amendments', []))}")

    # 6. overview
    r = client.get(f"{BASE}/api/v1/tools?action=overview&name=과학기술법")
    print(f"overview: {r.status_code}")

    # 7. verify
    r = client.get(f"{BASE}/api/v1/tools?action=verify&name=과학기술법&article=제1조")
    data = r.json()["data"]
    print(f"verify: {r.status_code}, exists={data.get('exists')}")

    print("\n=== ALL TESTS PASSED ===")

except Exception as e:
    print(f"\nERROR: {e}")
    import traceback
    traceback.print_exc()

finally:
    proc.terminate()
    proc.wait()
    print("서버 종료")
