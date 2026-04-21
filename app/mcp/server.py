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
) -> dict:
    """북한 법률을 검색합니다. mode: hybrid, keyword, semantic"""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.search(query, mode=mode, category=category, limit=limit)
    finally:
        await session.close()


@mcp.tool()
async def law_get(
    name: str,
    article: str | None = None,
    grep: str | None = None,
) -> dict:
    """북한 법률의 조문을 조회합니다."""
    session, law_svc, _ = await _get_services()
    try:
        return await law_svc.get(name, article=article, grep=grep)
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
) -> dict:
    """남북한 법률 매핑 목록을 조회합니다. category로 분류 필터링 가능."""
    factory = get_session_factory()
    session = factory()
    try:
        return await mapping_service.list_mappings(
            session, category=category, limit=limit
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
