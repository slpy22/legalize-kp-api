from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.core.config import get_config
from app.core.database import get_session_factory, get_qdrant
from app.repositories.law_repo import LawRepository
from app.repositories.pg_search import PgSearchRepository
from app.repositories.qdrant_search import QdrantSearchRepository
from app.services.search_engine import SearchEngine
from app.services.law_service import LawService
from app.services.tools_service import ToolsService

cfg = get_config()
mcp = FastMCP(cfg.get("mcp", {}).get("server_name", "legalize-kp"))


async def _get_services():
    """Create a session and build all services."""
    factory = get_session_factory()
    session = factory()

    law_repo = LawRepository(session)
    pg_search = PgSearchRepository(session)

    qdrant_client = get_qdrant()
    collection = cfg.get("qdrant", {}).get("collection", "legalize_kp_laws")
    qdrant_search = QdrantSearchRepository(qdrant_client, collection)

    embed_fn = None
    try:
        from google import genai

        genai_client = genai.Client()
        model = cfg.get("embedding", {}).get("model", "text-embedding-004")

        def embed_fn(text: str) -> list[float]:
            return genai_client.models.embed_content(
                model=model, contents=[text]
            ).embeddings[0].values
    except Exception:
        pass

    search_engine = SearchEngine(pg_search, qdrant_search, embed_fn)
    law_service = LawService(law_repo, search_engine)
    tools_service = ToolsService(law_repo, search_engine)
    return session, law_service, tools_service


@mcp.tool()
async def law_search(
    query: str,
    mode: str = "hybrid",
    category: str | None = None,
    limit: int = 20,
    page: int = 1,
    per_page: int = 10,
    articles_per_law: int = 5,
) -> dict:
    """북한 법률을 검색합니다. mode: hybrid, keyword, semantic.

    per_page 가 한 페이지의 법령 수 상한(기본 10), articles_per_law 가 법령별 매칭 조문 수
    상한(기본 5)입니다. 더 많이 받으려면 두 값을 올리세요(예: per_page=50, articles_per_law=20).
    """
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.search(
            query, mode=mode, category=category, limit=limit,
            page=page, per_page=per_page, articles_per_law=articles_per_law,
        )
    finally:
        await session.close()


@mcp.tool()
async def law_get(
    name: str,
    article: str | None = None,
    grep: str | None = None,
    version: str | None = None,
) -> dict:
    """북한 법률의 조문을 조회합니다.

    name 은 정식명·별칭·약칭('헌법') 모두 허용. version='YYYY-MM-DD' 를 주면 그 판본(law_versions)
    의 전문/조문을 돌려줍니다(보유 판본은 law_versions 도구로 확인). 응답 meta 에 출처 설명·별칭·
    날짜이견(date_disputes), 조문에 편(part)/장(chapter)/절(section)이 포함됩니다.
    """
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.get(name, article=article, grep=grep, version=version)
    finally:
        await session.close()


@mcp.tool()
async def law_versions(name: str) -> dict:
    """북한 법률의 보유 판본(본문 있는 버전) 목록 — 일자·action·출처(설명 포함)."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.list_versions(name)
    finally:
        await session.close()


@mcp.tool()
async def law_diff_text(name: str, date1: str, date2: str) -> dict:
    """같은 법률의 두 판본(date1 → date2) 본문·조문을 나란히 돌려줍니다(조문 번호 기준 비교용)."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.diff_text(name, from_date=date1, to_date=date2)
    finally:
        await session.close()


@mcp.tool()
async def law_diff_semantic(
    name: str,
    date1: str,
    date2: str,
    match_threshold: float = 0.78,
) -> dict:
    """두 판본의 조문을 의미(임베딩 유사도)로 짝지어 신설/삭제/변경/동일로 분류합니다.

    조문 번호가 밀려도 내용이 가까우면 같은 조항으로 대응시킵니다(match_threshold 기본 0.78).
    """
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.diff_semantic(
            name, from_date=date1, to_date=date2, match_threshold=match_threshold
        )
    finally:
        await session.close()


@mcp.tool()
async def law_list(
    category: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """전체 법령 목록(메타만)을 페이지네이션으로 돌려줍니다. 이름을 모르는 법령 탐색용."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.list_laws(category=category, offset=offset, limit=limit)
    finally:
        await session.close()


@mcp.tool()
async def law_resolve(name: str) -> dict:
    """이름·별칭·약칭을 정식 법령명으로 해석합니다(모호하면 후보 목록)."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.resolve_name(name)
    finally:
        await session.close()


@mcp.tool()
async def law_sources() -> dict:
    """출처 코드(mobu/nis/unification)가 어느 법령집·판인지 설명합니다."""
    session, law_svc, _ = await _get_services()
    try:
        return law_svc.list_sources()
    finally:
        await session.close()


@mcp.tool()
async def law_history(name: str) -> dict:
    """북한 법률의 개정 이력을 조회합니다."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.history(name)
    finally:
        await session.close()


@mcp.tool()
async def law_diff(
    name: str,
    date1: str | None = None,
    date2: str | None = None,
) -> dict:
    """북한 법률의 개정 내역을 기간별로 조회합니다."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.diff(name, date1=date1, date2=date2)
    finally:
        await session.close()


@mcp.tool()
async def tools_overview(
    name: str | None = None,
    query: str | None = None,
) -> dict:
    """법률 개요 및 관련 법률을 조회합니다."""
    session, _, tools_svc = await _get_services()
    try:
        return await tools_svc.overview(name=name, query=query)
    finally:
        await session.close()


@mcp.tool()
async def tools_verify(
    name: str,
    article: str | None = None,
) -> dict:
    """법률/조문의 존재 여부와 신뢰도를 확인합니다."""
    session, _, tools_svc = await _get_services()
    try:
        return await tools_svc.verify(name, article=article)
    finally:
        await session.close()


@mcp.tool()
async def tools_compare(
    kp_name: str,
    kr_query: str,
) -> dict:
    """남북한 법률을 비교합니다. kp_name: 북한 법률명, kr_query: 한국 법률 검색어"""
    session, _, tools_svc = await _get_services()
    try:
        return await tools_svc.compare(kp_name, kr_query)
    finally:
        await session.close()


# ── Compare tools ──────────────────────────────────────────────

from app.compare import mapping_service, term_service
from app.compare.article_compare import compare_articles as _compare_articles
from app.compare.structure_compare import compare_structure as _compare_structure
from app.compare.beopmang_client import KrLawClient


@mcp.tool()
async def compare_mapping(
    category: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """남북한 법률 매핑 목록을 조회합니다. category 로 분류 필터링, offset/limit 으로 전체 순회 가능."""
    factory = get_session_factory()
    session = factory()
    try:
        return await mapping_service.list_mappings(
            session, category=category, limit=limit, per_page=limit, offset=offset
        )
    finally:
        await session.close()


@mcp.tool()
async def compare_detail(kp_name: str) -> dict:
    """특정 북한 법률의 남한 법률 매핑 상세를 조회합니다."""
    factory = get_session_factory()
    session = factory()
    try:
        return await mapping_service.get_mapping(session, kp_name)
    finally:
        await session.close()


@mcp.tool()
async def compare_articles(kp_name: str, kr_name: str) -> dict:
    """남북한 법률의 조문을 조별로 비교합니다."""
    factory = get_session_factory()
    session = factory()
    client = KrLawClient()
    try:
        return await _compare_articles(session, kp_name, kr_name, client)
    finally:
        await client.close()
        await session.close()


@mcp.tool()
async def compare_terms(
    query: str | None = None,
    category: str | None = None,
) -> dict:
    """남북한 법률 용어 대조표를 검색합니다."""
    factory = get_session_factory()
    session = factory()
    try:
        return await term_service.search_terms(
            session, query=query, category=category
        )
    finally:
        await session.close()


@mcp.tool()
async def compare_structure(kp_name: str, kr_name: str) -> dict:
    """남북한 법률의 장/편 구조를 비교합니다."""
    factory = get_session_factory()
    session = factory()
    client = KrLawClient()
    try:
        return await _compare_structure(session, kp_name, kr_name, client)
    finally:
        await client.close()
        await session.close()
