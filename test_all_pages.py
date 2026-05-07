"""전체 페이지 + API 엔드포인트 점검."""
import httpx

API = "http://localhost:8000"
WEB = "http://localhost:3000"

print("=== API 엔드포인트 ===")
api_tests = [
    ("/api/v1/help?action=schema", "help"),
    ("/api/v1/ref", "ref (카테고리)"),
    ("/api/v1/ref?category=헌법", "ref (헌법)"),
    ("/api/v1/law?action=search&q=과학&mode=keyword&page=1&per_page=10", "search"),
    ("/api/v1/law?action=get&name=과학기술법", "get law"),
    ("/api/v1/law?action=history&name=과학기술법", "history"),
    ("/api/v1/law?action=diff&name=과학기술법", "diff"),
    ("/api/v1/tools?action=overview&name=과학기술법", "overview"),
    ("/api/v1/tools?action=verify&name=과학기술법&article=1", "verify"),
    ("/api/v1/compare/?action=mapping&page=1&per_page=10", "compare mapping"),
    ("/api/v1/compare/?action=detail&kp_name=과학기술법", "compare detail"),
    ("/api/v1/compare/?action=terms&page=1&per_page=10", "compare terms"),
    ("/api/v1/compare/?action=structure&kp_name=과학기술법&kr_name=과학기술기본법", "compare structure"),
    ("/api/v1/compare/?action=articles&kp_name=과학기술법&kr_name=과학기술기본법", "compare articles"),
]

for path, name in api_tests:
    try:
        r = httpx.get(f"{API}{path}", timeout=15, follow_redirects=True)
        status = "OK" if r.status_code == 200 else f"ERR {r.status_code}"
        print(f"  [{status}] {name}")
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")

print("\n=== 웹 페이지 ===")
web_tests = [
    ("/", "홈"),
    ("/category/헌법", "카테고리 (헌법)"),
    ("/law/과학기술법", "법령 상세"),
    ("/law/과학기술법/history", "개정이력"),
    ("/search?q=과학&mode=keyword", "검색"),
    ("/diff", "신구대조"),
    ("/compare", "남북법비교 목록"),
    ("/compare/과학기술법", "남북법 상세비교"),
    ("/compare/과학기술법/articles?kr_name=과학기술기본법", "조문비교"),
    ("/compare/과학기술법/structure?kr_name=과학기술기본법", "체계비교"),
    ("/compare/terms", "용어대조표"),
    ("/stats", "통계"),
    ("/chat", "AI 챗봇"),
]

for path, name in web_tests:
    try:
        r = httpx.get(f"{WEB}{path}", timeout=15, follow_redirects=True)
        status = "OK" if r.status_code == 200 else f"ERR {r.status_code}"
        # 500 에러 시 에러 내용 확인
        if r.status_code >= 400:
            err_text = r.text[:200] if "error" in r.text.lower() else ""
            print(f"  [{status}] {name} {err_text}")
        else:
            print(f"  [{status}] {name}")
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
