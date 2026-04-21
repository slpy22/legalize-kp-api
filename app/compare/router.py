from __future__ import annotations

import math
import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.api.schemas import make_response
from app.compare.beopmang_client import KrLawClient
from app.compare import mapping_service, term_service
from app.compare.article_compare import compare_articles as _compare_articles
from app.compare.structure_compare import compare_structure as _compare_structure

router = APIRouter(prefix="/api/v1/compare")


@router.get("/")
async def compare_endpoint(
    action: str = Query("mapping"),
    kp_name: str = Query(None),
    kr_name: str = Query(None),
    category: str = Query(None),
    confidence: str = Query(None),
    query: str = Query(None),
    q: str = Query(None),
    limit: int = Query(50),
    page: int = Query(1),
    per_page: int = Query(10),
    session: AsyncSession = Depends(get_session),
):
    t0 = time.time()
    search_query = q or query

    if action == "mapping":
        data = await mapping_service.list_mappings(
            session, category=category, confidence=confidence, q=search_query,
            limit=limit, page=page, per_page=per_page,
        )
        total = data.get("total", 0)
        total_pages = math.ceil(total / per_page) if per_page > 0 else 1
        return make_response(
            data, t0, total=total,
            page=page, per_page=per_page, total_pages=total_pages,
        )

    elif action == "detail":
        if not kp_name:
            return make_response({"error": "kp_name parameter required"}, t0)
        data = await mapping_service.get_mapping(session, kp_name)
        return make_response(data, t0)

    elif action == "articles":
        if not kp_name or not kr_name:
            return make_response(
                {"error": "kp_name and kr_name parameters required"}, t0
            )
        client = KrLawClient()
        try:
            data = await _compare_articles(session, kp_name, kr_name, client)
        finally:
            await client.close()
        return make_response(
            data, t0, total=len(data.get("article_pairs", []))
        )

    elif action == "terms":
        data = await term_service.search_terms(
            session, query=search_query, category=category, limit=limit,
            page=page, per_page=per_page,
        )
        total = data.get("total", 0)
        total_pages = math.ceil(total / per_page) if per_page > 0 else 1
        return make_response(
            data, t0, total=total,
            page=page, per_page=per_page, total_pages=total_pages,
        )

    elif action == "structure":
        if not kp_name or not kr_name:
            return make_response(
                {"error": "kp_name and kr_name parameters required"}, t0
            )
        client = KrLawClient()
        try:
            data = await _compare_structure(session, kp_name, kr_name, client)
        finally:
            await client.close()
        return make_response(data, t0)

    else:
        return make_response({"error": f"Unknown action: {action}"}, t0)
