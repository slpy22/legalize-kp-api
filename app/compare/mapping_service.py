from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def list_mappings(
    session: AsyncSession,
    category: str | None = None,
    confidence: str | None = None,
    q: str | None = None,
    limit: int = 50,
    page: int = 1,
    per_page: int = 10,
    offset: int | None = None,
) -> dict:
    """Return compare_mappings rows (N:M schema) with optional filters, search, and pagination.

    offset 을 주면 page 대신 그 위치부터 per_page 개를 돌려준다(MCP offset/limit 용, T4 (b)).
    """
    clauses: list[str] = []
    params: dict = {}

    if category:
        clauses.append("kp_category = :category")
        params["category"] = category
    if confidence is not None:
        clauses.append("confidence = :confidence")
        params["confidence"] = confidence
    if q:
        clauses.append("(kp_name ILIKE :q OR :q_raw = ANY(kr_names) OR EXISTS (SELECT 1 FROM unnest(kr_names) AS kr WHERE kr ILIKE :q))")
        params["q"] = f"%{q}%"
        params["q_raw"] = q

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    count_result = await session.execute(
        text(f"SELECT count(*) FROM compare_mappings {where}"), params
    )
    total = count_result.scalar() or 0

    if offset is None:
        offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = max(0, int(offset))

    result = await session.execute(
        text(
            f"SELECT id, kp_name, kp_category, kr_names, kr_categories, "
            f"relationship, overlap_areas, confidence, source, notes "
            f"FROM compare_mappings {where} "
            f"ORDER BY kp_name LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    rows = result.mappings().all()

    mappings = []
    for r in rows:
        row = dict(r)
        # Ensure array fields are lists (SQLAlchemy may return them as-is)
        for arr_field in ("kr_names", "kr_categories", "overlap_areas"):
            if row.get(arr_field) is None:
                row[arr_field] = []
        mappings.append(row)

    return {"total": total, "mappings": mappings}


async def get_mapping(session: AsyncSession, kp_name: str) -> dict:
    """Return a single mapping by kp_name, including all N:M fields."""
    result = await session.execute(
        text("SELECT * FROM compare_mappings WHERE kp_name = :kp_name"),
        {"kp_name": kp_name},
    )
    row = result.mappings().first()
    if row is None:
        # 매핑 미작성은 오류가 아니라 정상 상태(관리 결정 T7 §9-③). 신규 법령(관광법·교육후원법·
        # 각급 인민회의 대의원법, T6 미보유 13종 등)은 비교 매핑 생성 일감 전까지 이 상태다.
        # 소비처(웹·챗봇·MCP)가 바로 쓸 수 있게 빈 필드를 채워 돌려준다.
        return {
            "kp_name": kp_name,
            "mapped": False,
            "status": "no_mapping",
            "message": f"'{kp_name}' 의 남북 비교 매핑이 아직 작성되지 않았습니다(오류가 아닌 미작성 상태).",
            "kr_names": [], "kr_categories": [], "overlap_areas": [],
            "kp_unique": [], "kr_unique": [], "article_mappings": [],
            "relationship": None, "confidence": None, "source": None, "notes": None,
        }

    data = dict(row)
    data["mapped"] = True
    data["status"] = "mapped"

    # Ensure array fields are lists
    for arr_field in ("kr_names", "kr_categories", "overlap_areas", "kp_unique", "kr_unique"):
        if data.get(arr_field) is None:
            data[arr_field] = []

    # Parse article_mappings JSONB (may already be a list from the driver)
    am = data.get("article_mappings")
    if am is None:
        data["article_mappings"] = []
    elif isinstance(am, str):
        try:
            data["article_mappings"] = json.loads(am)
        except (json.JSONDecodeError, TypeError):
            data["article_mappings"] = []

    return data
