# legalize-kp API

북한 법령 검색 및 분석 API 서버 (REST + MCP)

## 설치

```bash
pip install -r requirements.txt
```

## 실행

### REST API 서버

```bash
python main.py
```

서버가 `http://localhost:8000` 에서 시작됩니다.

### MCP stdio 모드

```bash
python mcp_stdio.py
```

## API 엔드포인트

| 엔드포인트 | 주요 파라미터 |
|---|---|
| `GET /api/v1/law?action=search&q=...` | 법률 검색 |
| `GET /api/v1/law?action=get&name=...` | 법률 조문 조회 |
| `GET /api/v1/law?action=history&name=...` | 개정 이력 |
| `GET /api/v1/law?action=diff&name=...` | 개정 비교 |
| `GET /api/v1/tools?action=overview` | 법률 개요 |
| `GET /api/v1/tools?action=verify&name=...` | 존재 확인 |
| `GET /api/v1/tools?action=compare&kp_name=...&kr_query=...` | 남북한 비교 |
| `GET /api/v1/ref` | 분류 목록 |
| `GET /api/v1/help` | API 스키마 |
| `/mcp` | MCP Streamable HTTP |

## MCP 도구

`law_search`, `law_get`, `law_history`, `law_diff`, `tools_overview`, `tools_verify`, `tools_compare`
