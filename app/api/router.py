from __future__ import annotations

import math
import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.database import get_session, get_qdrant
from app.repositories.law_repo import LawRepository
from app.repositories.pg_search import PgSearchRepository
from app.repositories.qdrant_search import QdrantSearchRepository
from app.services.search_engine import SearchEngine
from app.services.law_service import LawService
from app.services.tools_service import ToolsService
from app.api.schemas import make_response

router = APIRouter(prefix="/api/v1")


# --- 임베딩 클라이언트 (앱 시작 시 1회만 초기화) ---
_embed_fn_cache = None
_embed_fn_initialized = False


def _get_embed_fn():
    """임베딩 함수를 캐싱하여 반환. 최초 1회만 초기화."""
    global _embed_fn_cache, _embed_fn_initialized
    if _embed_fn_initialized:
        return _embed_fn_cache
    _embed_fn_initialized = True
    try:
        import os
        from google import genai

        cfg = get_config()
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("[WARN] GOOGLE_API_KEY not set — semantic search disabled")
            return None
        genai_client = genai.Client(api_key=api_key)
        model = cfg.get("embedding", {}).get("model", "gemini-embedding-001")

        def embed_fn(text: str) -> list[float]:
            return genai_client.models.embed_content(
                model=model, contents=[text]
            ).embeddings[0].values

        _embed_fn_cache = embed_fn
        print(f"[INFO] Embedding ready: model={model}")
    except Exception as e:
        print(f"[WARN] Embedding not available: {e}")
        _embed_fn_cache = None
    return _embed_fn_cache


def _build_services(session: AsyncSession):
    """Build all service instances from a session."""
    cfg = get_config()
    law_repo = LawRepository(session)
    pg_search = PgSearchRepository(session)

    qdrant_client = get_qdrant()
    collection = cfg.get("qdrant", {}).get("collection", "legalize_kp_laws")
    qdrant_search = QdrantSearchRepository(qdrant_client, collection)

    embed_fn = _get_embed_fn()

    search_engine = SearchEngine(pg_search, qdrant_search, embed_fn)
    law_service = LawService(law_repo, search_engine)
    tools_service = ToolsService(law_repo, search_engine)
    return law_service, tools_service, law_repo


@router.get("/law")
async def law_endpoint(
    action: str = Query("search"),
    q: str = Query(None),
    name: str = Query(None),
    mode: str = Query("hybrid"),
    category: str = Query(None),
    limit: int = Query(20),
    page: int = Query(1),
    per_page: int = Query(10),
    article: str = Query(None),
    grep: str = Query(None),
    date1: str = Query(None),
    date2: str = Query(None),
    session: AsyncSession = Depends(get_session),
):
    t0 = time.time()
    law_svc, _, _ = _build_services(session)

    if action == "search":
        if not q:
            return make_response({"error": "q parameter required"}, t0)
        data = await law_svc.search(
            q, mode=mode, category=category, limit=limit,
            page=page, per_page=per_page,
        )
        total = data.get("total", 0)
        total_pages = math.ceil(total / per_page) if per_page > 0 else 1
        return make_response(
            data, t0, total=total,
            page=page, per_page=per_page, total_pages=total_pages,
        )

    elif action == "get":
        if not name:
            return make_response({"error": "name parameter required"}, t0)
        data = await law_svc.get(name, article=article, grep=grep)
        return make_response(data, t0, total=data.get("total_articles", 0))

    elif action == "history":
        if not name:
            return make_response({"error": "name parameter required"}, t0)
        data = await law_svc.history(name)
        return make_response(data, t0, total=len(data.get("amendments", [])))

    elif action == "diff":
        if not name:
            return make_response({"error": "name parameter required"}, t0)
        data = await law_svc.diff(name, date1=date1, date2=date2)
        return make_response(data, t0, total=data.get("total", 0))

    else:
        return make_response({"error": f"Unknown action: {action}"}, t0)


@router.get("/tools")
async def tools_endpoint(
    action: str = Query("overview"),
    name: str = Query(None),
    q: str = Query(None),
    query: str = Query(None),
    article: str = Query(None),
    kp_name: str = Query(None),
    kr_query: str = Query(None),
    session: AsyncSession = Depends(get_session),
):
    t0 = time.time()
    _, tools_svc, _ = _build_services(session)
    search_query = q or query

    if action == "overview":
        data = await tools_svc.overview(name=name, query=search_query)
        return make_response(data, t0)

    elif action == "verify":
        if not name:
            return make_response({"error": "name parameter required"}, t0)
        data = await tools_svc.verify(name, article=article)
        return make_response(data, t0)

    elif action == "compare":
        if not kp_name or not kr_query:
            return make_response(
                {"error": "kp_name and kr_query parameters required"}, t0
            )
        data = await tools_svc.compare(kp_name, kr_query)
        return make_response(data, t0)

    else:
        return make_response({"error": f"Unknown action: {action}"}, t0)


@router.get("/ref")
async def ref_endpoint(
    q: str = Query(None),
    category: str = Query(None),
    limit: int = Query(20),
    page: int = Query(1),
    per_page: int = Query(10),
    session: AsyncSession = Depends(get_session),
):
    t0 = time.time()
    law_svc, _, law_repo = _build_services(session)

    if q:
        data = await law_svc.search(
            q, mode="keyword", category=category, limit=limit,
            page=page, per_page=per_page,
        )
        total = data.get("total", 0)
        total_pages = math.ceil(total / per_page) if per_page > 0 else 1
        return make_response(
            data, t0, total=total,
            page=page, per_page=per_page, total_pages=total_pages,
        )

    if category:
        all_laws = await law_repo.list_by_category(category)
        total = len(all_laws)
        total_pages = math.ceil(total / per_page) if per_page > 0 else 1
        offset = (page - 1) * per_page
        laws = all_laws[offset:offset + per_page]
        return make_response(
            {"category": category, "laws": laws}, t0, total=total,
            page=page, per_page=per_page, total_pages=total_pages,
        )

    categories = await law_repo.list_categories()
    count = await law_repo.count_laws()
    return make_response(
        {"total_laws": count, "categories": categories},
        t0,
        total=count,
    )


@router.get("/help")
async def help_endpoint(
    action: str = Query("schema"),
):
    t0 = time.time()

    if action == "schema":
        data = {
            "api_version": "v1.0.0",
            "endpoints": {
                "/api/v1/law": {
                    "actions": ["search", "get", "history", "diff"],
                    "params": {
                        "search": {"q": "검색어(필수)", "mode": "hybrid|keyword|semantic", "category": "분류", "limit": "결과수", "page": "페이지(기본1)", "per_page": "페이지당건수(기본10)"},
                        "get": {"name": "법률명(필수)", "article": "조번호", "grep": "본문검색어"},
                        "history": {"name": "법률명(필수)"},
                        "diff": {"name": "법률명(필수)", "date1": "시작일", "date2": "종료일"},
                    },
                },
                "/api/v1/tools": {
                    "actions": ["overview", "verify", "compare"],
                    "params": {
                        "overview": {"name": "법률명", "q": "검색어"},
                        "verify": {"name": "법률명(필수)", "article": "조번호"},
                        "compare": {"kp_name": "북한법률명(필수)", "kr_query": "한국법검색어(필수)"},
                    },
                },
                "/api/v1/compare/": {
                    "actions": ["mapping", "detail", "terms", "articles", "structure"],
                    "params": {
                        "mapping": {"category": "분류", "confidence": "신뢰도필터", "limit": "결과수", "page": "페이지(기본1)", "per_page": "페이지당건수(기본10)"},
                        "detail": {"kp_name": "북한법률명(필수)"},
                        "terms": {"q": "검색어", "category": "분류", "limit": "결과수", "page": "페이지(기본1)", "per_page": "페이지당건수(기본10)"},
                        "articles": {"kp_name": "북한법률명(필수)", "kr_name": "한국법률명(필수)"},
                        "structure": {"kp_name": "북한법률명(필수)", "kr_name": "한국법률명(필수)"},
                    },
                },
                "/api/v1/ref": {
                    "params": {"q": "검색어", "category": "분류", "limit": "결과수", "page": "페이지(기본1)", "per_page": "페이지당건수(기본10)"},
                },
                "/api/v1/help": {
                    "actions": ["schema"],
                },
            },
        }
        return make_response(data, t0)

    return make_response({"error": f"Unknown action: {action}"}, t0)
