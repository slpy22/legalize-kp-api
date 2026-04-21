from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def search_terms(
    session: AsyncSession,
    query: str | None = None,
    category: str | None = None,
    limit: int = 50,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Search compare_terms by keyword or category with pagination."""
    clauses: list[str] = []
    params: dict = {}

    if query:
        clauses.append("(kp_term ILIKE :q OR kr_term ILIKE :q)")
        params["q"] = f"%{query}%"
    if category:
        clauses.append("category = :category")
        params["category"] = category

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    count_result = await session.execute(
        text(f"SELECT count(*) FROM compare_terms {where}"), params
    )
    total = count_result.scalar() or 0

    offset = (page - 1) * per_page
    params["limit"] = per_page
    params["offset"] = offset

    result = await session.execute(
        text(
            f"SELECT * FROM compare_terms {where} "
            f"ORDER BY kp_term LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    rows = result.mappings().all()
    return {"total": total, "terms": [dict(r) for r in rows]}
