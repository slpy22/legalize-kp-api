from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.law_repo import LawRepository
from app.repositories.pg_search import PgSearchRepository
from app.compare.mapping_service import get_mapping
from app.chat.term_converter import expand_query

SYSTEM_PROMPT = """당신은 북한법 전문 AI 연구 에이전트입니다.
310개의 북한 법령과 모든 조문에 직접 접근할 수 있습니다.

## 작동 방식 (ReAct Agent)
당신은 단순 챗봇이 아니라 **자율적으로 조사하는 연구 에이전트**입니다.
복잡한 질문에는 다음 패턴을 반복하세요:

1. **계획**: 어떤 법령/조문을 조사할지 결정
2. **실행**: 도구를 호출하여 실제 데이터 수집
3. **관찰**: 결과를 분석하고 추가 조사 필요 여부 판단
4. **반복**: 충분한 근거가 모일 때까지 2-3을 반복
5. **종합**: 수집한 모든 근거를 종합하여 체계적으로 답변

## 절대 금지
- 추측하거나 일반 지식으로 답변하지 마세요
- "접근할 수 없다", "확인이 어렵다", "제한적이다" 같은 말 금지
- 도구를 한 번만 호출하고 포기하지 마세요. 여러 번 호출하세요.

## 도구 활용 전략
- **search_laws**: 관련 법령 목록 파악 (남한어 입력 → 자동 문화어 변환)
- **search_articles**: 조문 내용을 키워드로 직접 검색 — 가장 강력한 도구!
  - "금지", "처벌", "의무", "승인", "통제", "보장" 등으로 검색
  - law_name 지정으로 특정 법령 내 검색 가능
- **get_article**: 특정 법령의 조문 전문 조회
- **compare_laws**: 남북법 대응 관계 조회
- **lookup_term**: 남한어↔북한어 용어 조회

## 복잡한 질문 처리 예시
"과학기술법의 기본권 제한 조항" 질문 시:
→ search_articles(query="금지", law_name="과학기술법")
→ search_articles(query="의무", law_name="과학기술법")
→ search_articles(query="처벌", law_name="과학기술법")
→ search_articles(query="승인", law_name="과학기술법")
→ get_article(law_name="과학기술법", article="제87조")  // 전문 필요 시
→ 종합 답변

## 답변 형식
- 마크다운으로 체계적으로 작성
- 모든 주장에 **출처(법령명, 조문번호)** 필수 인용
- 조문 원문을 인용하고 분석을 추가
- 한국어로 답변"""

TOOL_DECLARATIONS = [
    {
        "name": "search_laws",
        "description": "북한 법령 데이터베이스(310개)에서 키워드로 법령을 검색합니다. 남한어로 입력하면 자동으로 북한 문화어로도 검색됩니다.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "검색어 (남한어 가능). 예: '과학기술', '소프트웨어 보호', '정보통신'"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": "특정 법령의 조문 내용을 조회합니다. article을 생략하면 전체 조문 목록을 반환합니다. 법령 내용을 직접 읽어야 할 때 사용하세요.",
        "parameters": {
            "type": "object",
            "properties": {
                "law_name": {"type": "string", "description": "법령명 (정확한 이름). 예: '과학기술법'"},
                "article": {"type": "string", "description": "조문번호. 예: '제1조'. 생략하면 전체 조문 반환"},
            },
            "required": ["law_name"],
        },
    },
    {
        "name": "search_articles",
        "description": "법령 조문 내용을 키워드로 직접 검색합니다. 특정 주제(예: '금지', '처벌', '의무', '통제', '승인')가 포함된 조항을 찾을 때 사용하세요. law_name을 지정하면 해당 법령 내에서만 검색합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "조문 내용 검색어. 예: '금지', '처벌', '국가 승인', '의무'"},
                "law_name": {"type": "string", "description": "특정 법령 내에서만 검색 (선택). 예: '과학기술법'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "compare_laws",
        "description": "북한법과 대응하는 남한법을 비교합니다",
        "parameters": {
            "type": "object",
            "properties": {"kp_name": {"type": "string"}},
            "required": ["kp_name"],
        },
    },
    {
        "name": "lookup_term",
        "description": "남한어↔북한어(문화어) 용어를 조회합니다",
        "parameters": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
]


async def execute_tool(name: str, args: dict, session: AsyncSession) -> dict:
    """Execute a tool by name and return {result, sources}."""
    try:
        if name == "search_laws":
            return await _search_laws(args, session)
        elif name == "get_article":
            return await _get_article(args, session)
        elif name == "search_articles":
            return await _search_articles(args, session)
        elif name == "compare_laws":
            return await _compare_laws(args, session)
        elif name == "lookup_term":
            return await _lookup_term(args, session)
        else:
            return {"result": f"Unknown tool: {name}", "sources": []}
    except Exception as e:
        return {"result": f"tool error: {str(e)}", "sources": []}


async def _search_laws(args: dict, session: AsyncSession) -> dict:
    query = args.get("query", "")
    repo = PgSearchRepository(session)

    # 1) 용어변환 적용 (남한어 → 북한어)
    expanded = expand_query(query)

    # 2) 변환된 단어들만 추출 (원본 제외)
    converted_only = expanded.replace(query, "").strip() if expanded != query else ""

    # 3) 검색 전략: 변환어 우선, 원본 보충
    rows = []
    seen_ids: set = set()

    # 변환어로 먼저 검색 (예: "쏘프트웨어 보호")
    if converted_only:
        r1, _ = await repo.search_laws(converted_only, limit=5)
        for r in r1:
            if r.get("id") not in seen_ids:
                rows.append(r)
                seen_ids.add(r.get("id"))

    # 원본으로 보충 검색
    r2, _ = await repo.search_laws(query, limit=5)
    for r in r2:
        if r.get("id") not in seen_ids:
            rows.append(r)
            seen_ids.add(r.get("id"))

    total = len(rows)
    rows = rows[:5]

    sources = []
    lines = []
    for r in rows:
        name = r.get("name", "")
        category = r.get("category", "")
        sources.append({"law_name": name, "category": category})
        lines.append(f"- {name} ({category})")

    result_text = f"검색 결과 ({total}건 중 상위 5건):\n" + "\n".join(lines) if lines else "검색 결과가 없습니다."
    return {"result": result_text, "sources": sources}


async def _get_article(args: dict, session: AsyncSession) -> dict:
    import re
    law_name = args.get("law_name", "")
    article = args.get("article")

    # "제1조" → "1", "제10조" → "10" 변환
    if article:
        m = re.search(r"(\d+)", article)
        if m:
            article = m.group(1)

    repo = LawRepository(session)
    law = await repo.get_by_name(law_name)
    if not law:
        return {"result": f"법령을 찾을 수 없습니다: {law_name}", "sources": []}

    articles = await repo.get_articles(law["id"], article_number=article)
    if not articles:
        msg = f"{law_name}에서 조문을 찾을 수 없습니다"
        if article:
            msg += f" ({article})"
        return {"result": msg, "sources": [{"law_name": law_name}]}

    sources = [{"law_name": law_name, "article": a.get("article_number", "")} for a in articles]
    lines = []
    # 특정 조문 지정 시 전문 반환, 전체 조회 시 요약
    max_articles = 10 if article else 30
    for a in articles[:max_articles]:
        num = a.get("article_number", "")
        title = a.get("article_title", "")
        content = a.get("content", "")
        header = f"{num} {title}".strip()
        # 전체 조회 시 200자로 요약, 특정 조문은 전문
        if not article and len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"[{header}]\n{content}")

    return {"result": "\n\n".join(lines), "sources": sources}


async def _search_articles(args: dict, session: AsyncSession) -> dict:
    """조문 내용을 키워드로 직접 검색."""
    query = args.get("query", "")
    law_name = args.get("law_name")
    expanded = expand_query(query)

    # 단어 분리
    words = expanded.split()
    if not words:
        return {"result": "검색어를 입력하세요.", "sources": []}

    # ILIKE 조건: 모든 단어가 포함된 조문 (AND)
    conditions = " AND ".join(
        f"(a.content ILIKE :w{i} OR a.article_title ILIKE :w{i})" for i in range(len(words))
    )
    params: dict = {}
    for i, w in enumerate(words):
        params[f"w{i}"] = f"%{w}%"

    law_filter = ""
    if law_name:
        law_filter = "AND l.name = :law_name"
        params["law_name"] = law_name

    sql = f"""
        SELECT a.article_number, a.article_title, a.content, l.name as law_name
        FROM articles a JOIN laws l ON a.law_id = l.id
        WHERE ({conditions}) {law_filter}
        ORDER BY l.name, a.position
        LIMIT 20
    """
    result = await session.execute(text(sql), params)
    rows = result.mappings().all()

    if not rows:
        # AND 실패 시 OR로 폴백
        or_conditions = " OR ".join(
            f"(a.content ILIKE :w{i} OR a.article_title ILIKE :w{i})" for i in range(len(words))
        )
        sql_or = f"""
            SELECT a.article_number, a.article_title, a.content, l.name as law_name
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE ({or_conditions}) {law_filter}
            ORDER BY l.name, a.position
            LIMIT 20
        """
        result = await session.execute(text(sql_or), params)
        rows = result.mappings().all()

    if not rows:
        return {"result": f"'{query}'가 포함된 조문을 찾을 수 없습니다.", "sources": []}

    sources = []
    lines = []
    for r in rows:
        lname = r["law_name"]
        num = r["article_number"]
        title = r.get("article_title", "")
        content = r["content"]
        if len(content) > 200:
            content = content[:200] + "..."
        header = f"{lname} 제{num}조 {title}".strip()
        lines.append(f"[{header}]\n{content}")
        sources.append({"law_name": lname, "article": str(num)})

    result_text = f"조문 검색 결과 ({len(rows)}건):\n\n" + "\n\n".join(lines)
    return {"result": result_text, "sources": sources}


async def _compare_laws(args: dict, session: AsyncSession) -> dict:
    kp_name = args.get("kp_name", "")
    mapping = await get_mapping(session, kp_name)

    if "error" in mapping:
        return {"result": mapping["error"], "sources": []}

    kr_names = mapping.get("kr_names", [])
    relationship = mapping.get("relationship", "")
    overlap = mapping.get("overlap_areas", [])
    confidence = mapping.get("confidence", "")

    lines = [
        f"북한법: {kp_name}",
        f"대응 남한법: {', '.join(kr_names)}",
        f"관계: {relationship}",
        f"신뢰도: {confidence}",
    ]
    if overlap:
        lines.append(f"중복 영역: {', '.join(overlap)}")

    sources = [{"law_name": kp_name, "type": "compare"}]
    return {"result": "\n".join(lines), "sources": sources}


async def _lookup_term(args: dict, session: AsyncSession) -> dict:
    term = args.get("term", "")

    result = await session.execute(
        text(
            "SELECT * FROM compare_terms "
            "WHERE kp ILIKE :t OR kr ILIKE :t "
            "LIMIT 10"
        ),
        {"t": f"%{term}%"},
    )
    rows = result.mappings().all()

    if not rows:
        return {"result": f"'{term}'에 대한 용어 매핑을 찾을 수 없습니다.", "sources": []}

    lines = []
    for r in rows:
        kp = r.get("kp", "") or r.get("term_kp", "")
        kr = r.get("kr", "") or r.get("term_kr", "")
        cat = r.get("category", "")
        lines.append(f"- {kp} (북) ↔ {kr} (남) [{cat}]")

    return {"result": "\n".join(lines), "sources": [{"type": "term_lookup", "term": term}]}
