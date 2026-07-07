from __future__ import annotations

import logging
import os
import re

import httpx

from app.repositories.law_repo import LawRepository
from app.services.search_engine import SearchEngine

logger = logging.getLogger("law_service")

# ---------------------------------------------------------------------------
# 자체 에이전트(Session Manager) — 개정 diff 분석 리포트를 구독 기반 Claude 로 생성.
# Gemini 종량 호출을 대체(비용절감). 챗봇 세션(b8748f86)은 프롬프트 8000자 제한이 있어
# 재사용 불가 → diff 근거(수만 자)를 담을 수 있는 전용 '생성기' 세션을 별도로 둔다.
#   1) GET  {BASE}/api/v1/sessions?label=<라벨> → session_id 해석
#   2) POST {BASE}/api/v1/sessions/{id}/ask     → 원샷 fork 질의(원본 불변, fork 자동삭제)
# 실패(SM 다운·세션 없음·오류) 시 호출측이 Gemini 로 폴백한다.
# ---------------------------------------------------------------------------
_SM_BASE = os.environ.get("SM_RESUME_BASE", "http://host.docker.internal:5100").rstrip("/")
_SM_TOKEN = os.environ.get("SM_API_TOKEN", "")
_SM_GEN_LABEL = os.environ.get("SM_GEN_LABEL", "북한법 생성기")


async def _sm_find_gen_session() -> str | None:
    """_SM_GEN_LABEL 을 라벨(tags) 또는 표시이름(name)으로 갖는 세션 id 반환."""
    headers = {"Authorization": f"Bearer {_SM_TOKEN}"} if _SM_TOKEN else {}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_SM_BASE}/api/v1/sessions",
            params={"label": _SM_GEN_LABEL, "limit": 1}, headers=headers,
        )
        r.raise_for_status()
        ss = r.json().get("sessions", [])
        if ss:
            return ss[0]["session_id"]
        r = await client.get(
            f"{_SM_BASE}/api/v1/sessions", params={"limit": 0}, headers=headers,
        )
        r.raise_for_status()
        for s in r.json().get("sessions", []):
            if s.get("label_name") == _SM_GEN_LABEL or _SM_GEN_LABEL in (s.get("labels") or []):
                return s["session_id"]
    return None


async def _generate_report_via_self(prompt: str) -> str:
    """전용 생성기 세션(구독 Claude)에 원샷 요청해 리포트 마크다운을 반환.

    프로필(SM측)에 분석가 시스템프롬프트·프롬프트 80000자 허용·모든 도구 차단이 설정돼 있다.
    실패하면 예외를 던져 호출측이 Gemini 로 폴백하게 한다.
    """
    sid = await _sm_find_gen_session()
    if not sid:
        raise RuntimeError(f"생성기 세션('{_SM_GEN_LABEL}')을 찾을 수 없습니다.")
    headers = {"Authorization": f"Bearer {_SM_TOKEN}"} if _SM_TOKEN else {}
    async with httpx.AsyncClient(timeout=240) as client:
        r = await client.post(
            f"{_SM_BASE}/api/v1/sessions/{sid}/ask",
            json={"prompt": prompt, "timeout": 200}, headers=headers,
        )
        r.raise_for_status()
        data = r.json()
    if data.get("is_error"):
        raise RuntimeError(f"생성기 오류: {str(data.get('answer') or data.get('result'))[:120]}")
    ans = (data.get("answer") or "").strip()
    if not ans:
        raise RuntimeError("생성기 빈 응답")
    return ans


def _norm_ws(s: str | None) -> str:
    """본문 동일성 판정용 정규화 — 연속 공백/줄바꿈을 공백 1개로, 양끝 공백 제거.

    공백 정도의 차이(입력 오타·줄바꿈 차이)는 '변경'이 아닌 '동일'로 보기 위함.
    """
    return re.sub(r"\s+", " ", (s or "").strip())


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

    async def diff_semantic(
        self,
        name: str,
        from_date: str,
        to_date: str,
        match_threshold: float = 0.78,
    ) -> dict:
        """의미(임베딩) 기반으로 두 버전 조문을 짝짓고 신설/삭제/변경/동일로 분류.

        절차:
          1. 두 버전의 조문 + 적재된 임베딩 벡터를 가져온다.
          2. 코사인 유사도 행렬을 만들고 greedy 로 최적 매칭한다.
             (조문 번호가 바뀌어도 의미가 가까우면 같은 조항으로 본다)
          3. 매칭된 쌍: 본문 동일→same, 다름→modified.
             미매칭 from→removed, 미매칭 to→added.
          변경된 조항의 텍스트 단위 비교는 클라이언트가 수행한다.
        """
        import numpy as np

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

        from_articles = v_from.get("articles") or []
        to_articles = v_to.get("articles") or []

        # 벡터 로드 (적재된 임베딩 재사용)
        qrepo = self.search_engine.qdrant_search
        from_vecs = qrepo.get_version_vectors(int(v_from["id"]))
        to_vecs = qrepo.get_version_vectors(int(v_to["id"]))

        def _key(a: dict) -> str:
            return str(a.get("article_number"))

        # 벡터가 있는 조문만 의미 매칭 대상. 없으면 번호 기반 폴백.
        use_semantic = bool(from_vecs) and bool(to_vecs)

        pairs: list[dict] = []
        matched_from: set[int] = set()
        matched_to: set[int] = set()

        if use_semantic:
            fa = [a for a in from_articles if _key(a) in from_vecs]
            ta = [a for a in to_articles if _key(a) in to_vecs]
            fmat = np.array([from_vecs[_key(a)] for a in fa], dtype=float)
            tmat = np.array([to_vecs[_key(a)] for a in ta], dtype=float)

            if len(fa) and len(ta):
                # 정규화 후 코사인 유사도 행렬
                fnorm = fmat / (np.linalg.norm(fmat, axis=1, keepdims=True) + 1e-9)
                tnorm = tmat / (np.linalg.norm(tmat, axis=1, keepdims=True) + 1e-9)
                sim = fnorm @ tnorm.T  # (F, T)

                # greedy: 유사도 높은 쌍부터 매칭
                flat = [
                    (sim[i, j], i, j)
                    for i in range(sim.shape[0])
                    for j in range(sim.shape[1])
                ]
                flat.sort(key=lambda x: x[0], reverse=True)
                for s, i, j in flat:
                    if s < match_threshold:
                        break
                    if i in matched_from or j in matched_to:
                        continue
                    matched_from.add(i)
                    matched_to.add(j)
                    f = fa[i]
                    t = ta[j]
                    kind = "same" if _norm_ws(f.get("content")) == _norm_ws(t.get("content")) else "modified"
                    pairs.append({
                        "kind": kind,
                        "similarity": round(float(s), 4),
                        "from": f,
                        "to": t,
                    })

            # 미매칭 처리
            for i, f in enumerate(fa):
                if i not in matched_from:
                    pairs.append({"kind": "removed", "from": f, "to": None})
            for j, t in enumerate(ta):
                if j not in matched_to:
                    pairs.append({"kind": "added", "from": None, "to": t})

            # 벡터 없던 조문(누락)도 번호 기반으로 보강
            fa_keys = {_key(a) for a in fa}
            ta_keys = {_key(a) for a in ta}
            for a in from_articles:
                if _key(a) not in fa_keys:
                    pairs.append({"kind": "removed", "from": a, "to": None})
            for a in to_articles:
                if _key(a) not in ta_keys:
                    pairs.append({"kind": "added", "from": None, "to": a})
        else:
            # 폴백: 조문 번호 기반 매칭
            from_by = {_key(a): a for a in from_articles}
            to_by = {_key(a): a for a in to_articles}
            for k, f in from_by.items():
                t = to_by.get(k)
                if t is None:
                    pairs.append({"kind": "removed", "from": f, "to": None})
                else:
                    kind = "same" if _norm_ws(f.get("content")) == _norm_ws(t.get("content")) else "modified"
                    pairs.append({"kind": kind, "from": f, "to": t})
            for k, t in to_by.items():
                if k not in from_by:
                    pairs.append({"kind": "added", "from": None, "to": t})

        # 정렬: to(신본) 조문번호 우선, 없으면 from 번호
        def _sort_num(p: dict):
            a = p.get("to") or p.get("from") or {}
            try:
                return (0, int(a.get("article_number")))
            except (TypeError, ValueError):
                return (1, 0)

        pairs.sort(key=_sort_num)

        summary = {
            "added": sum(1 for p in pairs if p["kind"] == "added"),
            "removed": sum(1 for p in pairs if p["kind"] == "removed"),
            "modified": sum(1 for p in pairs if p["kind"] == "modified"),
            "same": sum(1 for p in pairs if p["kind"] == "same"),
        }

        return {
            "law_name": law["name"],
            "method": "semantic" if use_semantic else "article_number",
            "match_threshold": match_threshold,
            "from": {
                "version_date": v_from["version_date"],
                "action": v_from.get("action"),
                "source": v_from.get("source"),
            },
            "to": {
                "version_date": v_to["version_date"],
                "action": v_to.get("action"),
                "source": v_to.get("source"),
            },
            "summary": summary,
            "pairs": pairs,
        }

    async def diff_report(
        self,
        name: str,
        from_date: str,
        to_date: str,
        match_threshold: float = 0.78,
    ) -> dict:
        """diff_semantic 결과를 LLM으로 의미론적으로 종합하여 체계적 변화 리포트(마크다운) 생성."""
        import os

        diff = await self.diff_semantic(
            name, from_date, to_date, match_threshold=match_threshold
        )
        if "error" in diff:
            return diff

        pairs = diff.get("pairs", [])
        summary = diff.get("summary", {})

        def _trim(s: str | None, n: int = 400) -> str:
            s = (s or "").strip().replace("\n", " ")
            return s[:n] + ("…" if len(s) > n else "")

        modified_lines: list[str] = []
        added_lines: list[str] = []
        removed_lines: list[str] = []
        for p in pairs:
            kind = p.get("kind")
            f = p.get("from") or {}
            t = p.get("to") or {}
            if kind == "modified":
                fn = f.get("article_number")
                tn = t.get("article_number")
                head = f"제{fn}조→제{tn}조" if fn != tn else f"제{tn}조"
                modified_lines.append(
                    f"- [{head}] (유사도 {p.get('similarity')})\n"
                    f"  · 이전: {_trim(f.get('content'))}\n"
                    f"  · 신본: {_trim(t.get('content'))}"
                )
            elif kind == "added":
                added_lines.append(f"- 제{t.get('article_number')}조: {_trim(t.get('content'))}")
            elif kind == "removed":
                removed_lines.append(f"- 제{f.get('article_number')}조: {_trim(f.get('content'))}")

        evidence = (
            f"### 변경된 조항 ({len(modified_lines)}건)\n"
            + ("\n".join(modified_lines) if modified_lines else "(없음)")
            + f"\n\n### 신설된 조항 ({len(added_lines)}건)\n"
            + ("\n".join(added_lines) if added_lines else "(없음)")
            + f"\n\n### 삭제된 조항 ({len(removed_lines)}건)\n"
            + ("\n".join(removed_lines) if removed_lines else "(없음)")
        )
        if len(evidence) > 60000:
            evidence = evidence[:60000] + "\n…(이하 생략)"

        prompt = f"""당신은 북한법 비교법학 전문가입니다.
아래는 「{diff['law_name']}」의 두 시점({from_date} → {to_date}) 사이 조문별 변화 데이터입니다.
조문은 의미(임베딩 유사도) 기반으로 매칭되었으며, 번호가 달라도 의미가 같으면 한 쌍으로 묶여 있습니다.

{evidence}

---
위 데이터를 **의미론적으로** 종합하여, 단순 조문 나열이 아닌 체계적인 변화 리포트를 작성하세요.
다음 구조를 따르되, 내용이 없는 절은 생략하세요. 반드시 한국어 마크다운으로 작성하고 조문번호를 근거로 인용하세요.

## 1. 개요
- 이번 개정의 전체 규모와 성격을 2~4문장으로 요약 (변경 {summary.get('modified',0)} · 신설 {summary.get('added',0)} · 삭제 {summary.get('removed',0)})

## 2. 주제·영역별 주요 변화
- 변화를 의미 단위로 그룹핑하라 (예: 국가이념·지도사상, 영토·주권, 경제제도, 공민의 권리·의무, 국가기구, 대외관계 등 — 해당 법에 맞는 영역으로).
- 각 영역에서 무엇이 어떻게 바뀌었는지 분석하고 함의를 서술하라.

## 3. 신설된 제도·조항의 함의
- 새로 도입된 핵심 내용과 그 의미.

## 4. 삭제된 제도·조항의 함의
- 빠진 핵심 내용과 그 의미.

## 5. 종합 평가
- 이번 개정이 드러내는 방향성·특징을 통찰력 있게 정리.
"""

        # 1차: 자체 에이전트(구독 Claude, 전용 생성기 세션) — 비용절감 목적.
        report_md = ""
        try:
            report_md = await _generate_report_via_self(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("자체 에이전트 리포트 실패 → Gemini 폴백: %s", e)

        # 2차(폴백): 자체 에이전트 불가(SM 다운·세션 없음·오류) 시에만 Gemini.
        if not report_md.strip():
            try:
                from google import genai
                from google.genai import types as gtypes

                client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
                resp = client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(
                        system_instruction=(
                            "당신은 북한법·비교법 전문 분석가입니다. 제공된 변화 데이터에만 근거하여 "
                            "의미 중심의 체계적 리포트를 작성합니다. 추측하지 말고 데이터에 충실하되, "
                            "법적·제도적 함의를 통찰력 있게 해석하세요."
                        ),
                    ),
                )
                report_md = resp.text or ""
            except Exception as e:  # noqa: BLE001
                report_md = f"리포트 생성에 실패했습니다: {str(e)[:150]}"

        return {
            "law_name": diff["law_name"],
            "from": diff["from"],
            "to": diff["to"],
            "summary": summary,
            "method": diff.get("method"),
            "report": report_md,
        }

    async def history(self, name: str) -> dict:
        law = await self.law_repo.get_by_name(name)
        if law is None:
            return {"error": f"법률을 찾을 수 없습니다: {name}"}

        # 개정 연혁(amendments)과 보유 본문 버전(law_versions)을 날짜 기준 합집합으로 통합.
        # amendments 는 본문 헤더에서 추출한 개정 연혁이고, law_versions 는 실제 보유 본문이다.
        # 둘이 어긋날 수 있어(예: 헤더 없는 당규약·형법은 amendments=0 이나 보유본문 다수)
        # 신구대조(law_versions 기반)와 개정이력(amendments 기반)이 불일치하던 문제를 해소한다.
        amendments = await self.law_repo.get_amendments(law["id"])
        versions = await self.law_repo.list_versions(law["id"])

        by_date: dict[str, dict] = {}
        for a in amendments:
            d = str(a.get("date"))
            by_date[d] = {
                "id": a.get("id"),
                "law_id": law["id"],
                "date": d,
                "action": a.get("action") or "수정보충",
                "basis": a.get("basis"),
                "has_text": False,
                "source": None,
            }
        for v in versions:
            d = str(v.get("version_date"))
            if d in by_date:
                by_date[d]["has_text"] = True
                by_date[d]["source"] = v.get("source")
            else:
                by_date[d] = {
                    "id": f"v{v.get('id')}",
                    "law_id": law["id"],
                    "date": d,
                    "action": v.get("action") or "수정보충",
                    "basis": None,
                    "has_text": True,
                    "source": v.get("source"),
                }

        merged = sorted(by_date.values(), key=lambda x: x["date"])
        return {
            "law_name": name,
            "amendments": merged,
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
