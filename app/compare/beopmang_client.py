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
        """법제처 Open API로 법령 상세 조회 (MST 파라미터 사용)."""
        if not self.lawgo_oc:
            return {}
        try:
            # 먼저 검색해서 법령일련번호 확인
            results = await self._search_lawgo(query)
            if not results:
                return {}

            law_name = results[0].get("law_name", query)
            law_id = results[0].get("law_id", "")

            # 법령 상세 조회 (MST 파라미터 사용)
            resp = await self._client.get(
                f"{self.lawgo_url}/lawService.do",
                params={
                    "OC": self.lawgo_oc,
                    "target": "law",
                    "type": "JSON",
                    "MST": law_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # 루트 키: "법령" (법제처 API 응답 구조)
            beopryeong = data.get("법령", data.get("Law", data))
            if isinstance(beopryeong, str):
                return {"law_name": law_name, "total_articles": 0, "source": "법제처"}
            # 중첩 구조 처리: {"법령": {"법령": {...}}} 가능
            if isinstance(beopryeong, dict) and "기본정보" not in beopryeong and "법령" in beopryeong:
                beopryeong = beopryeong["법령"]
            if not isinstance(beopryeong, dict):
                return {"law_name": law_name, "total_articles": 0, "source": "법제처"}

            # 기본정보 추출
            info = beopryeong.get("기본정보", {})
            if isinstance(info, list):
                info = info[0] if info else {}

            # 조문 추출
            jomun = beopryeong.get("조문", {})
            articles = []
            if isinstance(jomun, dict):
                articles = jomun.get("조문단위", [])
            if isinstance(articles, dict):
                articles = [articles]

            # 조문 파싱: 장 제목 추적 + 실제 조문 추출
            import re
            parsed_articles = []
            current_chapter = ""
            for a in articles:
                content = (a.get("조문내용", "") or "").strip()
                jomun_type = a.get("조문유형", "")
                num = a.get("조문번호", "")
                title = a.get("조문제목", "")

                # 편/장/절 제목: 조문유형이 "편장" 또는 내용이 "제N장" 패턴
                if jomun_type == "편장" or (not title and content and re.match(r"\s*제\d+[장편절관]", content)):
                    ch_match = re.search(r"(제\d+[장편절관]\S*(?:\s+\S+)*)", content.strip())
                    if ch_match:
                        current_chapter = ch_match.group(1).strip()
                    continue

                # 실제 조문
                if not content:
                    continue
                parsed_articles.append({
                    "article_number": f"제{num}조" if num else "",
                    "article_title": title,
                    "content": content,
                    "chapter": current_chapter,
                })

            return {
                "law_name": info.get("법령명_한글", law_name),
                "law_type": info.get("법령구분명", ""),
                "total_articles": len(parsed_articles),
                "source": "법제처",
                "articles": parsed_articles,
            }
        except Exception:
            return {}

    async def close(self):
        await self._client.aclose()
