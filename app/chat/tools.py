from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.law_repo import LawRepository
from app.repositories.pg_search import PgSearchRepository
from app.compare.mapping_service import get_mapping
from app.chat.term_converter import expand_query

SYSTEM_PROMPT = """당신은 북한법 전문 AI 연구 보조원입니다.

규칙:
1. 모든 답변에 반드시 출처(법령명, 조문번호)를 인용하세요.
2. 조문 내용을 정확히 인용하되, 사용자가 이해하기 쉽게 설명을 추가하세요.
3. 불확실한 내용은 "확인이 필요합니다"라고 명시하세요.
4. 남북법 비교 시 양쪽 법령을 모두 인용하세요.
5. 한국어로 답변하세요.
6. 반드시 도구를 사용하여 정확한 법령 정보를 조회한 후 답변하세요.
7. search_laws 도구 사용 시 쿼리는 남한어로 입력하세요 (시스템이 자동으로 북한 문화어로 변환합니다). 예: "소프트웨어 보호", "컴퓨터 네트워크", "노동법"
8. 검색 결과가 없으면 다른 키워드로 재검색하세요. 예: "소프트웨어 저작권" 대신 "소프트웨어 보호"
9. 답변은 마크다운 형식으로 하되, 불필요한 따옴표나 인용부호 없이 깔끔하게 작성하세요."""

TOOL_DECLARATIONS = [
    {
        "name": "search_laws",
        "description": "북한 법령을 검색합니다",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": "특정 법령의 조문을 조회합니다",
        "parameters": {
            "type": "object",
            "properties": {
                "law_name": {"type": "string"},
                "article": {"type": "string", "description": "조문번호 예: 제1조"},
            },
            "required": ["law_name"],
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
    expanded = expand_query(query)
    repo = PgSearchRepository(session)
    rows, total = await repo.search_laws(expanded, limit=5)

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
    for a in articles[:10]:
        num = a.get("article_number", "")
        title = a.get("article_title", "")
        content = a.get("content", "")
        header = f"{num} {title}".strip()
        lines.append(f"[{header}]\n{content}")

    return {"result": "\n\n".join(lines), "sources": sources}


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
