"""ADK FunctionTool로 사용할 북한법 도구 함수들.

ADK는 함수의 docstring, 파라미터 타입힌트, 이름을 자동으로 파싱하여
LLM에게 도구 설명을 전달합니다.
"""
from __future__ import annotations

from kiwipiepy import Kiwi
from sqlalchemy import text

from app.core.database import get_session_factory
from app.chat.term_converter import expand_query

_kiwi = Kiwi()


def _extract_nouns(query: str) -> list[str]:
    """kiwipiepy로 명사/고유명사 추출."""
    tokens = _kiwi.tokenize(query)
    nouns = []
    for t in tokens:
        if t.tag.startswith("NN"):  # NNG, NNP, NNB
            if len(t.form) >= 2:
                nouns.append(t.form)
    return nouns


async def _get_session():
    factory = get_session_factory()
    return factory()


async def search_laws(query: str) -> str:
    """북한 법령을 키워드로 검색합니다. 310개 법령 DB에서 관련 법령 목록을 반환합니다.
    남한어를 입력해도 자동으로 북한 문화어로 변환됩니다.

    Args:
        query: 검색어. 예: '과학기술', '소프트웨어 보호', '외국인 투자'

    Returns:
        관련 법령 목록 (이름, 카테고리, 조문수)
    """
    from app.repositories.pg_search import PgSearchRepository
    expanded = expand_query(query)
    session = await _get_session()
    try:
        repo = PgSearchRepository(session)
        rows, total = await repo.search_laws(expanded, limit=10)
        if not rows:
            return f"'{query}' 검색 결과가 없습니다."
        lines = [f"검색 결과 ({total}건 중 상위 {len(rows)}건):"]
        for r in rows:
            lines.append(f"- {r['name']} ({r.get('category','')}, {r.get('total_articles','')}조)")
        return "\n".join(lines)
    finally:
        await session.close()


async def get_article(law_name: str, article_number: str = "") -> str:
    """특정 북한 법령의 조문을 조회합니다. article_number를 비우면 전체 조문 목록을 반환합니다.

    Args:
        law_name: 법령명 (정확한 이름). 예: '과학기술법', '저작권법'
        article_number: 조문번호. 예: '제1조', '87'. 비우면 전체 조문 반환

    Returns:
        조문 내용 (조문번호, 제목, 본문)
    """
    import re
    from app.repositories.law_repo import LawRepository
    session = await _get_session()
    try:
        repo = LawRepository(session)
        law = await repo.get_by_name(law_name)
        if not law:
            return f"법령 '{law_name}'을 찾을 수 없습니다."

        art_num = None
        if article_number:
            m = re.search(r"(\d+)", str(article_number))
            if m:
                art_num = m.group(1)

        articles = await repo.get_articles(law["id"], article_number=art_num)
        if not articles:
            return f"{law_name}에서 조문을 찾을 수 없습니다."

        lines = []
        limit = 10 if art_num else 30
        for a in articles[:limit]:
            num = a.get("article_number", "")
            title = a.get("article_title", "")
            content = a.get("content", "")
            if not art_num and len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"[제{num}조 {title}]\n{content}")
        return "\n\n".join(lines)
    finally:
        await session.close()


async def search_articles(query: str, law_name: str = "") -> str:
    """법령 조문 내용을 키워드로 직접 검색합니다. 조문 본문에서 해당 키워드가 포함된 조항을 찾습니다.

    Args:
        query: 조문 내용 검색어. 예: '처벌', '금지', '승인', '의무'
        law_name: 특정 법령 내에서만 검색 (선택). 비우면 전체 법령 대상

    Returns:
        매칭된 조문 목록 (법령명, 조문번호, 내용 일부)
    """
    expanded = expand_query(query)
    words = expanded.split()
    if not words:
        return "검색어를 입력하세요."

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

    session = await _get_session()
    try:
        sql = f"""
            SELECT a.article_number, a.article_title, a.content, l.name as law_name
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE ({conditions}) {law_filter}
            ORDER BY l.name, a.position LIMIT 15
        """
        result = await session.execute(text(sql), params)
        rows = result.mappings().all()

        if not rows and len(words) > 1:
            or_cond = " OR ".join(
                f"(a.content ILIKE :w{i} OR a.article_title ILIKE :w{i})" for i in range(len(words))
            )
            sql2 = f"""
                SELECT a.article_number, a.article_title, a.content, l.name as law_name
                FROM articles a JOIN laws l ON a.law_id = l.id
                WHERE ({or_cond}) {law_filter}
                ORDER BY l.name, a.position LIMIT 15
            """
            result = await session.execute(text(sql2), params)
            rows = result.mappings().all()

        if not rows:
            return f"'{query}'가 포함된 조문을 찾을 수 없습니다."

        lines = [f"조문 검색 결과 ({len(rows)}건):"]
        for r in rows:
            content = r["content"][:200] + "..." if len(r["content"]) > 200 else r["content"]
            lines.append(f"[{r['law_name']} 제{r['article_number']}조 {r.get('article_title','')}]\n{content}")
        return "\n\n".join(lines)
    finally:
        await session.close()


async def search_articles_semantic(query: str) -> str:
    """의미 기반으로 관련 조문을 검색합니다 (벡터 유사도). 키워드가 정확히 일치하지 않아도 의미적으로 관련된 조문을 찾습니다.

    Args:
        query: 자연어 질문 또는 주제. 예: '기본권 침해 가능성이 있는 처벌 조항', '과학기술 분야 국가 통제'

    Returns:
        의미적으로 관련된 조문 목록
    """
    from app.core.database import get_qdrant
    from app.core.config import get_config
    import google.genai as genai
    import os

    try:
        # 임베딩 생성
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        cfg = get_config()
        model = cfg.get("embedding", {}).get("model", "gemini-embedding-001")

        resp = client.models.embed_content(model=model, contents=query)
        vector = resp.embeddings[0].values

        # Qdrant 검색
        from app.repositories.qdrant_search import QdrantSearchRepository
        qdrant = get_qdrant()
        collection = cfg.get("qdrant", {}).get("collection", "legalize_kp_laws")
        qdrant_repo = QdrantSearchRepository(qdrant, collection)
        results_raw = qdrant_repo.search(vector, limit=15)

        # 변환
        class _Hit:
            def __init__(self, d):
                self.score = d.get("score", 0)
                self.payload = d
        results = [_Hit(r) for r in results_raw]

        if not results:
            return f"'{query}'와 의미적으로 관련된 조문을 찾을 수 없습니다."

        lines = [f"시맨틱 검색 결과 ({len(results)}건):"]
        for hit in results:
            payload = hit.payload or {}
            name = payload.get("law_name", "")
            num = payload.get("article_number", "")
            title = payload.get("article_title", "")
            content = payload.get("content", payload.get("content_snippet", ""))
            if len(content) > 150:
                content = content[:150] + "..."
            lines.append(f"[{name} 제{num}조 {title}]\n{content}")
        result = "\n\n".join(lines)
        if len(result) > 3000:
            result = result[:3000] + "\n...(이하 생략)"
        return result
    except Exception as e:
        return f"시맨틱 검색 오류: {str(e)[:100]}"


async def compare_laws(kp_name: str) -> str:
    """북한법과 대응하는 남한법의 매핑 정보를 조회합니다.

    Args:
        kp_name: 북한 법령명. 예: '과학기술법', '저작권법'

    Returns:
        대응 남한법 목록, 관계 유형, 공통 영역
    """
    from app.compare.mapping_service import get_mapping
    session = await _get_session()
    try:
        mapping = await get_mapping(session, kp_name)
        if "error" in mapping:
            return mapping["error"]

        kr_names = mapping.get("kr_names", [])
        lines = [
            f"북한법: {kp_name}",
            f"대응 남한법: {', '.join(kr_names)}",
            f"관계: {mapping.get('relationship', '')}",
            f"신뢰도: {mapping.get('confidence', '')}",
        ]
        if mapping.get("overlap_areas"):
            lines.append(f"공통 영역: {', '.join(mapping['overlap_areas'])}")
        return "\n".join(lines)
    finally:
        await session.close()


async def get_kr_article(law_name: str, article_number: str = "") -> str:
    """남한 법령의 조문을 법제처 API로 조회합니다.

    Args:
        law_name: 남한 법령명. 예: '과학기술기본법', '저작권법'
        article_number: 조문번호 (선택). 예: '제1조', '32'. 비우면 전체 반환

    Returns:
        남한법 조문 내용
    """
    from app.compare.beopmang_client import KrLawClient
    client = KrLawClient()
    try:
        articles = await client.get_article_by_number(law_name, article_number or None)
        if not articles:
            return f"남한법 '{law_name}' 조문을 조회할 수 없습니다."

        lines = [f"남한법 '{law_name}' ({len(articles)}조):"]
        for a in articles[:15]:
            num = a.get("article_number", "")
            title = a.get("article_title", "")
            content = a.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"[{num} {title}]\n{content}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"남한법 조회 오류: {str(e)[:100]}"
    finally:
        await client.close()


async def lookup_term(term: str) -> str:
    """남한어↔북한어(문화어) 용어를 조회합니다.

    Args:
        term: 조회할 용어. 예: '소프트웨어', '컴퓨터', '인터넷'

    Returns:
        남북 용어 대조 결과
    """
    session = await _get_session()
    try:
        result = await session.execute(
            text("SELECT * FROM compare_terms WHERE kp_term ILIKE :t OR kr_term ILIKE :t LIMIT 10"),
            {"t": f"%{term}%"},
        )
        rows = result.mappings().all()
        if not rows:
            return f"'{term}'에 대한 용어 매핑을 찾을 수 없습니다."

        lines = []
        for r in rows:
            kp = r.get("kp_term", "")
            kr = r.get("kr_term", "")
            cat = r.get("category", "")
            lines.append(f"- {kp} (북) ↔ {kr} (남) [{cat}]")
        return "\n".join(lines)
    finally:
        await session.close()
