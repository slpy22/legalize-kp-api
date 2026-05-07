import httpx, json

BASE = "http://localhost:8000"

# 1. detail
r = httpx.get(f"{BASE}/api/v1/compare/?action=detail&kp_name=과학기술법", timeout=30, follow_redirects=True)
d = r.json()
print("=== detail API 응답 구조 ===")
print(f"status: {r.status_code}")
print(f"top keys: {list(d.keys())}")
data = d.get("data", {})
print(f"data keys: {list(data.keys())}")
print(f"kp_name: {data.get('kp_name')}")
print(f"kr_names: {data.get('kr_names')}")
print(f"relationship: {data.get('relationship')}")
print(f"overlap_areas: {data.get('overlap_areas', [])[:2]}")
print(f"article_mappings: {len(data.get('article_mappings', []))}")
if data.get('article_mappings'):
    print(f"  예시: {data['article_mappings'][0]}")

# 2. 웹에서 어떻게 호출하는지 확인 — lib/api.ts의 fetchCompareDetail
print("\n=== terms ===")
r2 = httpx.get(f"{BASE}/api/v1/compare/?action=terms&limit=3", timeout=30, follow_redirects=True)
print(f"terms: {r2.status_code}, keys: {list(r2.json().get('data', {}).keys())}")

print("\n=== structure ===")
r3 = httpx.get(f"{BASE}/api/v1/compare/?action=structure&kp_name=과학기술법&kr_name=과학기술기본법", timeout=30, follow_redirects=True)
print(f"structure: {r3.status_code}")
print(f"data: {json.dumps(r3.json().get('data', {}), ensure_ascii=False)[:300]}")
