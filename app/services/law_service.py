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
        self,
        name: str,
        article: str | None = None,
        grep: str | None = None,
        version: str | None = None,
    ) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        # 특정 버전 요청 — law_versions 테이블에서 본문 가져옴
        if version:
            v = await self.law_repo.get_version(law["id"], version)
            if v is None:
                return {
                    "error": f"해당 일자의 버전이 없습니다: {name} ({version})",
                    "available": (await self.law_repo.list_versions(law["id"])),
                }
            articles_list = v.get("articles") or []
            # article/grep 필터 (DB 인덱스 없이 in-memory)
            if article:
                articles_list = [a for a in articles_list if str(a.get("article_number")) == str(article)]
            if grep:
                articles_list = [a for a in articles_list if grep in (a.get("content") or "")]
            return {
                "law": law,
                "version": {
                    "version_date": v["version_date"],
                    "action": v["action"],
                    "source": v["source"],
                    "frontmatter": v.get("frontmatter") or {},
                    "full_text": v.get("full_text") or "",
                },
                "articles": articles_list,
                "total_articles": len(articles_list),
            }

        articles = await self.law_repo.get_articles(
            law["id"], article_number=article, grep=grep
        )
        return {
            "law": law,
            "articles": articles,
            "total_articles": len(articles),
        }

    async def list_versions(self, name: str) -> dict:
        """적재된 버전 메타 목록(일자/action/source)."""
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}
        versions = await self.law_repo.list_versions(law["id"])
        return {
            "law_name": law["name"],
            "versions": versions,
            "total": len(versions),
        }

    async def diff_text(
        self, name: str, from_date: str, to_date: str
    ) -> dict:
        """두 버전 본문을 함께 반환 — 클라이언트(혹은 후속 endpoint)가 diff 렌더링."""
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        v_from = await self.law_repo.get_version(law["id"], from_date)
        v_to = await self.law_repo.get_version(law["id"], to_date)

        available = None
        if v_from is None or v_to is None:
            available = await self.law_repo.list_versions(law["id"])

        if v_from is None:
            return {"error": f"from 버전 없음: {from_date}", "available": available}
        if v_to is None:
            return {"error": f"to 버전 없음: {to_date}", "available": available}

        return {
            "law_name": law["name"],
            "from": {
                "version_date": v_from["version_date"],
                "action": v_from.get("action"),
                "source": v_from.get("source"),
                "articles": v_from.get("articles") or [],
                "full_text": v_from.get("full_text") or "",
            },
            "to": {
                "version_date": v_to["version_date"],
                "action": v_to.get("action"),
                "source": v_to.get("source"),
                "articles": v_to.get("articles") or [],
                "full_text": v_to.get("full_text") or "",
            },
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
