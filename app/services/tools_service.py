from __future__ import annotations

import httpx

from app.repositories.law_repo import LawRepository
from app.services.search_engine import SearchEngine
from app.core.config import get_config


class ToolsService:
    def __init__(self, law_repo: LawRepository, search_engine: SearchEngine):
        self.law_repo = law_repo
        self.search_engine = search_engine

    async def overview(
        self, name: str | None = None, query: str | None = None
    ) -> dict:
        result: dict = {}

        if name:
            law = await self.law_repo.get_by_name(name)
            if law is None:
                return {"error": f"법률을 찾을 수 없습니다: {name}"}
            articles = await self.law_repo.get_articles(law["id"])
            result["law"] = law
            result["top_articles"] = articles[:10]
            result["total_articles"] = len(articles)

            # Find related laws via search
            related_data = await self.search_engine.search(
                name, mode="keyword", limit=5, per_page=5,
            )
            result["related_laws"] = [
                r for r in related_data["results"] if r.get("law_name") != name
            ][:5]

        elif query:
            search_data = await self.search_engine.search(
                query, mode="keyword", limit=10, per_page=10,
            )
            result["query"] = query
            result["results"] = search_data["results"]
        else:
            count = await self.law_repo.count_laws()
            categories = await self.law_repo.list_categories()
            result["total_laws"] = count
            result["categories"] = categories

        return result

    async def verify(
        self, name: str, article: str | None = None
    ) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {
                "exists": False,
                "law_name": name,
                "message": "해당 법률을 찾을 수 없습니다.",
            }

        result: dict = {
            "exists": True,
            "law_name": name,
            "category": law.get("category", ""),
            "source": "legalize-kp DB",
            "reliability": "high",
        }

        if article:
            articles = await self.law_repo.get_articles(
                law["id"], article_number=article
            )
            if articles:
                result["article_exists"] = True
                result["article"] = articles[0]
            else:
                result["article_exists"] = False
                result["message"] = f"제{article}조를 찾을 수 없습니다."

        return result

    async def compare(self, kp_name: str, kr_query: str) -> dict:
        # Get North Korean law
        kp_law = await self.law_repo.get_by_name(kp_name)
        if kp_law is None:
            return {"error": f"북한 법률을 찾을 수 없습니다: {kp_name}"}

        kp_articles = await self.law_repo.get_articles(kp_law["id"])

        # Get South Korean law from beopmang API
        cfg = get_config()
        base_url = cfg.get("external", {}).get(
            "beopmang_base_url", "https://api.beopmang.org/api/v4"
        )

        kr_result = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base_url}/law",
                    params={"action": "search", "q": kr_query, "mode": "keyword"},
                )
                if resp.status_code == 200:
                    kr_result = resp.json()
                else:
                    kr_result = {"error": f"beopmang API returned {resp.status_code}"}
        except httpx.TimeoutException:
            kr_result = {"error": "beopmang API timeout"}
        except Exception as e:
            kr_result = {"error": f"beopmang API error: {str(e)}"}

        return {
            "kp_law": {
                "name": kp_name,
                "category": kp_law.get("category", ""),
                "total_articles": len(kp_articles),
                "articles": kp_articles[:10],
            },
            "kr_law": kr_result,
        }
