from __future__ import annotations

from app.repositories.law_repo import LawRepository
from app.services.search_engine import SearchEngine


class LawService:
    def __init__(self, law_repo: LawRepository, search_engine: SearchEngine):
        self.law_repo = law_repo
        self.search_engine = search_engine

    async def search(
        self,
        query: str,
        mode: str = "hybrid",
        category: str | None = None,
        limit: int = 20,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        data = await self.search_engine.search(
            query, mode=mode, category=category, limit=limit,
            page=page, per_page=per_page,
        )
        return {
            "total": data["total"],
            "results": data["results"],
            "mode": mode,
        }

    async def get(
        self, name: str, article: str | None = None, grep: str | None = None
    ) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        articles = await self.law_repo.get_articles(
            law["id"], article_number=article, grep=grep
        )
        return {
            "law": law,
            "articles": articles,
            "total_articles": len(articles),
        }

    async def history(self, name: str) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        amendments = await self.law_repo.get_amendments(law["id"])
        return {
            "law_name": name,
            "amendments": amendments,
        }

    async def diff(
        self, name: str, date1: str | None = None, date2: str | None = None
    ) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        amendments = await self.law_repo.get_amendments(law["id"])

        if date1 or date2:
            filtered = []
            for a in amendments:
                d = str(a.get("date", ""))
                if date1 and d < date1:
                    continue
                if date2 and d > date2:
                    continue
                filtered.append(a)
            amendments = filtered

        return {
            "law_name": name,
            "date_range": {"from": date1, "to": date2},
            "amendments": amendments,
            "total": len(amendments),
        }
