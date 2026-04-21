from __future__ import annotations

from typing import Any, Callable

from app.repositories.pg_search import PgSearchRepository
from app.repositories.qdrant_search import QdrantSearchRepository


class SearchEngine:
    def __init__(
        self,
        pg_search: PgSearchRepository,
        qdrant_search: QdrantSearchRepository,
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        self.pg_search = pg_search
        self.qdrant_search = qdrant_search
        self.embed_fn = embed_fn

    async def search(
        self,
        query: str,
        mode: str = "hybrid",
        category: str | None = None,
        limit: int = 20,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        """Search and return paginated results with total count.

        When page/per_page are used, limit is ignored in favour of per_page.
        Returns {"results": [...], "total": int}.
        """
        # Fetch a generous window so we can count total matches
        fetch_limit = max(limit, 200)
        if mode == "keyword":
            all_results = await self._keyword_search(query, category, fetch_limit)
        elif mode == "semantic":
            all_results = await self._semantic_search(query, category, fetch_limit)
        else:
            all_results = await self._hybrid_search(query, category, fetch_limit)

        total = len(all_results)
        offset = (page - 1) * per_page
        page_results = all_results[offset:offset + per_page]
        return {"results": page_results, "total": total}

    async def _keyword_search(
        self, query: str, category: str | None, limit: int
    ) -> list[dict]:
        articles, _ = await self.pg_search.search_articles(query, limit=limit * 2)
        laws, _ = await self.pg_search.search_laws(query, category=category, limit=limit)

        # Group articles by law name
        grouped: dict[str, list[dict]] = {}
        for a in articles:
            law_name = a.get("law_name", "")
            grouped.setdefault(law_name, []).append(a)

        results = []
        seen_names: set[str] = set()

        # Add law-level results first
        for law in laws:
            name = law.get("name", "")
            seen_names.add(name)
            results.append(
                {
                    "law_name": name,
                    "category": law.get("category", ""),
                    "score": law.get("rank", 0),
                    "source": "keyword",
                    "matching_articles": grouped.get(name, [])[:5],
                }
            )

        # Add article-only matches
        for law_name, arts in grouped.items():
            if law_name not in seen_names:
                results.append(
                    {
                        "law_name": law_name,
                        "category": arts[0].get("category", ""),
                        "score": max(a.get("rank", 0) for a in arts),
                        "source": "keyword",
                        "matching_articles": arts[:5],
                    }
                )

        # Filter by category if specified
        if category:
            results = [r for r in results if r.get("category") == category]

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:limit]

    async def _semantic_search(
        self, query: str, category: str | None, limit: int
    ) -> list[dict]:
        if self.embed_fn is None:
            return []

        try:
            vector = self.embed_fn(query)
        except Exception:
            return []

        hits = self.qdrant_search.search(vector, limit=limit * 3, category=category)

        # 법령 단위로 그룹화 (키워드 검색과 같은 포맷)
        grouped: dict[str, dict] = {}
        for h in hits:
            name = h.get("law_name", "")
            if name not in grouped:
                grouped[name] = {
                    "law_name": name,
                    "category": h.get("category", ""),
                    "score": h.get("score", 0),
                    "source": "semantic",
                    "matching_articles": [],
                }
            grouped[name]["matching_articles"].append({
                "article_number": h.get("article_number", ""),
                "article_title": h.get("article_title", ""),
                "content": h.get("content_snippet", ""),
            })

        results = sorted(grouped.values(), key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def _hybrid_search(
        self, query: str, category: str | None, limit: int
    ) -> list[dict]:
        keyword_results = await self._keyword_search(query, category, limit)
        semantic_results = await self._semantic_search(query, category, limit)

        return self._rrf_merge(keyword_results, semantic_results, k=60, limit=limit)

    @staticmethod
    def _rrf_merge(
        list_a: list[dict], list_b: list[dict], k: int = 60, limit: int = 20
    ) -> list[dict]:
        """Reciprocal Rank Fusion."""
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        for rank, item in enumerate(list_a):
            key = item.get("law_name", "")
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            items[key] = item

        for rank, item in enumerate(list_b):
            key = item.get("law_name", "")
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            if key not in items:
                items[key] = item

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for key, score in ranked[:limit]:
            entry = items[key].copy()
            entry["score"] = score
            entry["source"] = "hybrid"
            results.append(entry)

        return results
