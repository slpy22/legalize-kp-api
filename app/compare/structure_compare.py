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
            "SELECT a.chapter, count(*) as article_count "
            "FROM articles a JOIN laws l ON a.law_id = l.id "
            "WHERE l.name = :name AND a.chapter IS NOT NULL AND a.chapter != '' "
            "GROUP BY a.chapter ORDER BY min(a.position)"
        ),
        {"name": kp_name},
    )
    kp_rows = result.mappings().all()
    kp_chapters = [
        {"chapter": r["chapter"], "article_count": r["article_count"]}
        for r in kp_rows
    ]

    # --- KR structure from law.go.kr / beopmang ---
    kr_chapters: list[dict] = []
    try:
        overview = await beopmang_client.get_law_overview(kr_name)
        if overview:
            # 법제처 API 응답에서 articles 목록이 있으면 chapter 추출
            articles = overview.get("articles", [])
            if articles:
                chapter_counts: dict[str, int] = {}
                for a in articles:
                    ch = a.get("chapter", "") or ""
                    if ch:
                        chapter_counts[ch] = chapter_counts.get(ch, 0) + 1
                kr_chapters = [{"chapter": ch, "article_count": cnt} for ch, cnt in chapter_counts.items()]
            else:
                # 구조 정보가 없으면 기본 정보만
                structure = overview.get("structure", overview.get("chapters", []))
                if isinstance(structure, list):
                    kr_chapters = structure
    except Exception:
        pass

    return {
        "kp": {"name": kp_name, "chapters": kp_chapters},
        "kr": {"name": kr_name, "chapters": kr_chapters},
    }
