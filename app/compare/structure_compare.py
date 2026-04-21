from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.compare.beopmang_client import KrLawClient


async def compare_structure(
    session: AsyncSession,
    kp_name: str,
    kr_name: str,
    beopmang_client: KrLawClient,
) -> dict:
    """Compare the chapter-level structure of a KP law vs a KR law."""
    # --- KP chapters from local DB ---
    result = await session.execute(
        text(
            "SELECT chapter, count(*) as article_count "
            "FROM articles WHERE law_name = :name "
            "GROUP BY chapter ORDER BY chapter"
        ),
        {"name": kp_name},
    )
    kp_rows = result.mappings().all()
    kp_chapters = [
        {"chapter": r["chapter"], "article_count": r["article_count"]}
        for r in kp_rows
    ]

    # --- KR structure from Beopmang overview ---
    overview = await beopmang_client.get_law_overview(kr_name)
    kr_chapters: list[dict] = []
    if overview:
        structure = overview.get("structure", overview.get("chapters", []))
        if isinstance(structure, list):
            kr_chapters = structure
        elif isinstance(structure, dict):
            kr_chapters = structure.get("chapters", [])

    return {
        "kp": {"name": kp_name, "chapters": kp_chapters},
        "kr": {"name": kr_name, "chapters": kr_chapters},
    }
