"""남한법 조회 클라이언트 — beopmang 1차 + 국가법령정보센터 폴백."""
from __future__ import annotations

import httpx

from app.core.config import get_config


class KrLawClient:
    """남한법 조회 클라이언트.

    1차: beopmang API (빠르고 AI 친화적)
    2차: 국가법령정보센터 API (법제처 공공 API, 안정적)
    """

    def __init__(self):
        cfg = get_config().get("external", {})
        self.beopmang_url = cfg.get("beopmang_base_url", "https://api.beopmang.org/api/v4")
        self.lawgo_url = cfg.get("lawgo_base_url", "https://www.law.go.kr/DRF")
        self.lawgo_oc = cfg.get("lawgo_oc", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    async def search_law(self, query: str) -> list:
        """남한법 검색. beopmang 시도 → 실패 시 법제처 폴백."""
        results = await self._search_beopmang(query)
        if results:
            return results
        return await self._search_lawgo(query)

    async def get_law_overview(self, query: str) -> dict:
        """남한법 종합 정보. beopmang 시도 → 실패 시 법제처 폴백."""
        data = await self._overview_beopmang(query)
        if data:
            return data
        return await self._overview_lawgo(query)

    # ── beopmang ──

    async def _search_beopmang(self, query: str) -> list:
        try:
            resp = await self._client.get(
                f"{self.beopmang_url}/law",
                params={"action": "search", "q": query, "mode": "keyword"},
            )
            resp.raise_for_status()
            return resp.json().get("data", {}).get("results", [])
        except Exception:
            return []

    async def _overview_beopmang(self, query: str) -> dict:
        try:
            resp = await self._client.get(
                f"{self.beopmang_url}/tools",
                params={"action": "overview", "q": query},
            )
            resp.raise_for_status()
            return resp.json().get("data", {})
        except Exception:
            return {}

    # ── 국가법령정보센터 (법제처) ──

    async def _search_lawgo(self, query: str) -> list:
        """법제처 Open API로 법령 검색."""
        if not self.lawgo_oc:
            return []
        try:
            resp = await self._client.get(
                f"{self.lawgo_url}/lawSearch.do",
                params={
                    "OC": self.lawgo_oc,
                    "target": "law",
                    "type": "JSON",
                    "query": query,
                    "display": "10",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            laws = data.get("LawSearch", {}).get("law", [])
            if isinstance(laws, dict):
                laws = [laws]

            return [
                {
                    "law_name": law.get("법령명한글", ""),
                    "law_id": law.get("법령일련번호", ""),
                    "law_type": law.get("법령구분명", ""),
                    "source": "법제처",
                }
                for law in laws
            ]
        except Exception:
            return []

    async def _overview_lawgo(self, query: str) -> dict:
        """법제처 Open API로 법령 상세 조회."""
        if not self.lawgo_oc:
            return {}
        try:
            # 먼저 검색해서 법령ID 확인
            results = await self._search_lawgo(query)
            if not results:
                return {}

            law_name = results[0].get("law_name", query)
            law_id = results[0].get("law_id", "")

            # 법령 상세 조회
            resp = await self._client.get(
                f"{self.lawgo_url}/lawService.do",
                params={
                    "OC": self.lawgo_oc,
                    "target": "law",
                    "type": "JSON",
                    "ID": law_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # 기본정보 추출
            info = data.get("기본정보", {})
            if isinstance(info, list):
                info = info[0] if info else {}

            # 조문 추출
            articles = data.get("조문", {}).get("조문단위", [])
            if isinstance(articles, dict):
                articles = [articles]

            return {
                "law_name": info.get("법령명_한글", law_name),
                "law_type": info.get("법령구분명", ""),
                "total_articles": len(articles),
                "source": "법제처",
                "articles": [
                    {
                        "article_number": a.get("조문번호", ""),
                        "article_title": a.get("조문제목", ""),
                        "content": a.get("조문내용", "")[:200],
                    }
                    for a in articles[:10]
                ],
            }
        except Exception:
            return {}

    async def close(self):
        await self._client.aclose()
