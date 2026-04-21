from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.compare.beopmang_client import KrLawClient


def _article_number(title: str) -> str | None:
    """Extract a numeric article key like '1', '2' from '제1조', '제2조'."""
    m = re.search(r"제(\d+)조", title)
    return m.group(1) if m else None


async def compare_articles(
    session: AsyncSession,
    kp_name: str,
    kr_name: str,
    beopmang_client: KrLawClient,
    qdrant=None,
    embed_cfg: dict | None = None,
) -> dict:
    """Compare articles of a KP law with a KR law, matching by position."""
    # --- KP articles from local DB ---
    result = await session.execute(
        text(
            "SELECT article_number, article_title, content "
            "FROM articles WHERE law_name = :name "
            "ORDER BY article_number"
        ),
        {"name": kp_name},
    )
    kp_rows = result.mappings().all()
    kp_articles = [
        {
            "number": r["article_number"],
            "title": r.get("article_title", ""),
            "content": r["content"],
        }
        for r in kp_rows
    ]

    # --- KR articles from Beopmang ---
    kr_results = await beopmang_client.search_law(kr_name)
    kr_articles: list[dict] = []
    if kr_results:
        # The first hit should contain article data
        first = kr_results[0] if isinstance(kr_results, list) else kr_results
        raw_articles = first.get("articles", [])
        for a in raw_articles:
            kr_articles.append(
                {
                    "number": a.get("article_number", ""),
                    "title": a.get("article_title", ""),
                    "content": a.get("content", ""),
                }
            )

    # --- Match by article number (제N조 ↔ 제N조) ---
    kp_by_num = {}
    for a in kp_articles:
        key = _article_number(str(a["number"])) or str(a["number"])
        kp_by_num[key] = a

    kr_by_num = {}
    for a in kr_articles:
        key = _article_number(str(a["number"])) or str(a["number"])
        kr_by_num[key] = a

    all_keys = list(dict.fromkeys(list(kp_by_num.keys()) + list(kr_by_num.keys())))

    pairs = []
    kp_unmatched = []
    kr_unmatched = []

    for key in all_keys:
        kp_art = kp_by_num.get(key)
        kr_art = kr_by_num.get(key)
        if kp_art and kr_art:
            pairs.append({"article": f"제{key}조", "kp": kp_art, "kr": kr_art})
        elif kp_art:
            kp_unmatched.append(kp_art)
        else:
            kr_unmatched.append(kr_art)

    return {
        "kp_name": kp_name,
        "kr_name": kr_name,
        "article_pairs": pairs,
        "kp_unmatched": kp_unmatched,
        "kr_unmatched": kr_unmatched,
    }
