"""서버 주도 에이전트 오케스트레이터.

LLM에게 계획/도구 호출을 맡기지 않고, 서버가 직접 오케스트레이션합니다.
1. 쿼리 분류 (결정론적)
2. 검색 전략 실행 (병렬)
3. 증거 번들 조립
4. LLM 종합 답변 생성 (도구 호출 없이 텍스트만)
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Awaitable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.law_repo import LawRepository
from app.repositories.pg_search import PgSearchRepository
from app.compare.mapping_service import get_mapping
from app.chat.term_converter import expand_query
from app.core.database import get_session_factory

# ── 법령명 캐시 ──
_law_name_cache: set[str] | None = None


async def _get_law_names(session: AsyncSession) -> set[str]:
    global _law_name_cache
    if _law_name_cache is not None:
        return _law_name_cache
    result = await session.execute(text("SELECT name FROM laws"))
    _law_name_cache = {r["name"] for r in result.mappings().all()}
    return _law_name_cache


# ── 쿼리 분류 ──

class QueryType:
    SINGLE_LAW = "single_law"          # 특정 법령 조회
    ARTICLE_LOOKUP = "article_lookup"  # 특정 조문 조회
    KEYWORD_SEARCH = "keyword_search"  # 주제 검색
    MULTI_LAW_ANALYSIS = "multi_law"   # 여러 법령 분석
    COMPARISON = "comparison"          # 남북법 비교
    TERM_LOOKUP = "term_lookup"        # 용어 조회
    THEMATIC_DEEP = "thematic_deep"    # 심층 주제 분석


@dataclass
class ClassifiedQuery:
    query_type: str
    original: str
    expanded: str
    law_names: list[str] = field(default_factory=list)
    article_number: str | None = None
    keywords: list[str] = field(default_factory=list)
    analysis_keywords: list[str] = field(default_factory=list)


# 심층 분석용 키워드 매핑
_ANALYSIS_KEYWORD_MAP = {
    "기본권": ["금지", "의무", "처벌", "승인", "통제", "제한", "허가"],
    "제한": ["금지", "의무", "처벌", "승인", "통제", "제한", "허가"],
    "처벌": ["처벌", "벌금", "제재", "위반", "책임"],
    "통제": ["통제", "감독", "검열", "승인", "허가", "금지"],
    "의무": ["의무", "하여야 한다", "보장", "책임"],
    "권리": ["권리", "보장", "보호", "자유"],
    "외국인": ["외국인", "외국", "합영", "합작", "투자"],
    "투자": ["투자", "외국인", "합영", "합작", "경제"],
    "환경": ["환경", "오염", "보호", "자연"],
    "노동": ["노동", "로동", "근로", "임금", "휴식"],
    "교육": ["교육", "학교", "양성", "훈련"],
    "정보": ["정보", "비밀", "공개", "전산", "망"],
}

_COMPARISON_KW = re.compile(r"비교|차이|남한|남북|대응|대비")
_TERM_KW = re.compile(r"용어|문화어|뭐라고|말하|번역|표현")
_ARTICLE_PAT = re.compile(r"제(\d+)조")
_DEEP_KW = re.compile(
    r"기본권|제한|처벌|통제|의무|권리|침해|위반|제재|감독|"
    r"어떻게.*작동|요소|분석|조항.*찾|찾아.*조항"
)

# ── 분석 프레임워크 템플릿 ──
_ANALYSIS_TEMPLATES = {
    "기본권_제한": {
        "trigger": re.compile(r"기본권|제한|처벌|통제|침해|위반"),
        "search_keywords": ["금지", "의무", "처벌", "승인", "통제", "제한", "허가"],
        "analysis_framework": (
            "다음 프레임워크로 분석하세요:\n"
            "1. 의무 조항 (시민/기관에 부과되는 의무)\n"
            "2. 금지/제한 조항 (명시적 금지사항)\n"
            "3. 승인/허가 조항 (국가 승인 없이 불가능한 활동)\n"
            "4. 처벌 조항 (위반 시 제재)\n"
            "5. 감독/통제 조항 (국가 감시 메커니즘)\n"
            "각 조항에 대해 기본권과의 관계를 분석하세요."
        ),
    },
    "남북_비교": {
        "trigger": re.compile(r"비교|남한|대비|차이|대응"),
        "requires_kr_law": True,
        "analysis_framework": (
            "다음 프레임워크로 비교하세요:\n"
            "1. 법의 목적/사명 비교\n"
            "2. 규율 범위 비교\n"
            "3. 개인 권리 보장 수준 비교\n"
            "4. 국가 역할/통제 수준 비교\n"
            "5. 처벌/제재 비교\n"
            "양쪽 조문을 나란히 인용하세요."
        ),
    },
    "투자_관련": {
        "trigger": re.compile(r"투자|외국인|합영|합작|경제지대"),
        "search_keywords": ["투자", "외국인", "합영", "합작", "경제지대", "특구"],
        "requires_kr_law": True,
        "analysis_framework": (
            "다음 프레임워크로 분석하세요:\n"
            "1. 투자 허용 범위 및 제한 업종\n"
            "2. 외국인 투자자 권리 보호\n"
            "3. 투자 인센티브 (세금, 토지)\n"
            "4. 분쟁 해결 메커니즘\n"
            "5. 특수경제지대 특례\n"
            "양쪽 조문을 나란히 인용하세요."
        ),
    },
    "노동_관련": {
        "trigger": re.compile(r"노동|근로|임금|로동|휴식"),
        "search_keywords": ["로동", "임금", "휴식", "보호", "안전"],
        "requires_kr_law": True,
        "analysis_framework": (
            "다음 프레임워크로 분석하세요:\n"
            "1. 노동자 기본 권리 (근로시간, 휴식, 임금)\n"
            "2. 노동 의무 및 규율\n"
            "3. 노동 보호 (안전, 건강)\n"
            "4. 여성/청소년 특별 보호\n"
            "5. 위반 시 제재\n"
            "양쪽 조문을 나란히 인용하세요."
        ),
    },
}


def _match_analysis_template(query: str) -> dict | None:
    """쿼리에 매칭되는 분석 템플릿 반환."""
    for name, tpl in _ANALYSIS_TEMPLATES.items():
        if tpl["trigger"].search(query):
            return {**tpl, "template_name": name}
    return None


async def classify_query(
    query: str,
    session: AsyncSession,
    history: list[dict] | None = None,
) -> ClassifiedQuery:
    """쿼리를 분류하고 엔티티를 추출. 대화 이력에서 문맥도 참고."""
    law_names = await _get_law_names(session)
    expanded = expand_query(query)

    cq = ClassifiedQuery(
        query_type=QueryType.KEYWORD_SEARCH,
        original=query,
        expanded=expanded,
    )

    # 0. 대화 이력에서 문맥 추출 (이전에 언급된 법령명 등)
    context_law_names: list[str] = []
    if history:
        for msg in history[-6:]:  # 최근 3턴 (user+assistant)
            content = msg.get("content", "")
            for name in law_names:
                if name in content and name not in context_law_names:
                    context_law_names.append(name)

    # 1. 법령명 매칭 (현재 쿼리에서)
    for name in law_names:
        if name in query:
            cq.law_names.append(name)

    # 1-1. 현재 쿼리에 법령명이 없으면 대화 이력에서 가져오기
    if not cq.law_names and context_law_names:
        # "그 법", "이 법", "위 법", "해당" 등 지시어가 있으면 이전 법령 참조
        has_reference = any(kw in query for kw in [
            "그 법", "이 법", "위 법", "해당", "거기", "그것", "이것",
            "같은 법", "그중", "위에", "아까", "방금",
        ])
        # 또는 쿼리가 매우 짧으면 (후속 질문일 가능성)
        if has_reference or len(query) < 20:
            cq.law_names = context_law_names[:3]

    # 2. 조문번호 매칭
    art_match = _ARTICLE_PAT.search(query)
    if art_match:
        cq.article_number = art_match.group(1)

    # 3. 키워드 추출 (법령명, 조문번호 제외)
    kw_text = query
    for name in cq.law_names:
        kw_text = kw_text.replace(name, "")
    kw_text = _ARTICLE_PAT.sub("", kw_text).strip()
    cq.keywords = [w for w in kw_text.split() if len(w) > 1]

    # 4. 분류
    if cq.law_names and cq.article_number:
        cq.query_type = QueryType.ARTICLE_LOOKUP
    elif _COMPARISON_KW.search(query):
        cq.query_type = QueryType.COMPARISON
    elif _TERM_KW.search(query):
        cq.query_type = QueryType.TERM_LOOKUP
    elif _DEEP_KW.search(query):
        cq.query_type = QueryType.THEMATIC_DEEP
        # 분석 키워드 확장
        for trigger, expansions in _ANALYSIS_KEYWORD_MAP.items():
            if trigger in query:
                cq.analysis_keywords.extend(expansions)
        if not cq.analysis_keywords:
            cq.analysis_keywords = ["금지", "의무", "처벌", "승인", "통제"]
        cq.analysis_keywords = list(dict.fromkeys(cq.analysis_keywords))  # dedup
    elif cq.law_names and len(cq.law_names) == 1:
        cq.query_type = QueryType.SINGLE_LAW
    elif len(cq.keywords) >= 2 or "관련" in query or "법령들" in query:
        cq.query_type = QueryType.MULTI_LAW_ANALYSIS
    else:
        cq.query_type = QueryType.KEYWORD_SEARCH

    return cq


# ── 증거 번들 ──

@dataclass
class EvidenceBundle:
    query_type: str
    steps: list[dict] = field(default_factory=list)
    evidence_text: str = ""
    sources: list[dict] = field(default_factory=list)
    analysis_framework: str = ""  # 분석 프레임워크 (LLM 프롬프트에 주입)

    def add_step(self, action: str, detail: str = ""):
        self.steps.append({"action": action, "detail": detail})


# ── 검색 전략 실행 ──

# 콜백 타입: 진행 상황 알림
ProgressCallback = Callable[[str, str], Awaitable[None]]  # (event_type, detail)


async def execute_strategy(
    cq: ClassifiedQuery,
    session: AsyncSession,
    on_progress: ProgressCallback | None = None,
) -> EvidenceBundle:
    """분류된 쿼리에 대한 검색 전략을 실행하고 증거 번들을 반환."""
    bundle = EvidenceBundle(query_type=cq.query_type)

    async def _progress(action: str, detail: str = ""):
        bundle.add_step(action, detail)
        if on_progress:
            await on_progress(action, detail)

    repo = LawRepository(session)
    pg = PgSearchRepository(session)

    if cq.query_type == QueryType.ARTICLE_LOOKUP:
        await _strategy_article_lookup(cq, repo, bundle, _progress)
    elif cq.query_type == QueryType.SINGLE_LAW:
        await _strategy_single_law(cq, repo, bundle, _progress)
    elif cq.query_type == QueryType.COMPARISON:
        await _strategy_comparison(cq, repo, pg, session, bundle, _progress)
    elif cq.query_type == QueryType.TERM_LOOKUP:
        await _strategy_term_lookup(cq, session, pg, bundle, _progress)
    elif cq.query_type == QueryType.THEMATIC_DEEP:
        await _strategy_thematic_deep(cq, repo, pg, session, bundle, _progress)
    elif cq.query_type == QueryType.MULTI_LAW_ANALYSIS:
        await _strategy_multi_law(cq, pg, session, bundle, _progress)
    else:
        await _strategy_keyword_search(cq, pg, session, bundle, _progress)

    return bundle


async def _strategy_article_lookup(cq, repo, bundle, progress):
    """특정 법령의 특정 조문 조회."""
    law_name = cq.law_names[0]
    await progress("조문 조회", f"{law_name} 제{cq.article_number}조")

    law = await repo.get_by_name(law_name)
    if not law:
        bundle.evidence_text = f"법령 '{law_name}'을 찾을 수 없습니다."
        return

    articles = await repo.get_articles(law["id"], article_number=cq.article_number)
    if not articles:
        bundle.evidence_text = f"{law_name}에서 제{cq.article_number}조를 찾을 수 없습니다."
        return

    lines = []
    for a in articles:
        num = a.get("article_number", "")
        title = a.get("article_title", "")
        content = a.get("content", "")
        lines.append(f"### {law_name} 제{num}조 {title}\n{content}")
        bundle.sources.append({"law_name": law_name, "article": str(num)})

    bundle.evidence_text = "\n\n".join(lines)


async def _strategy_single_law(cq, repo, bundle, progress):
    """특정 법령 전체 조문 조회."""
    law_name = cq.law_names[0]
    await progress("법령 조회", law_name)

    law = await repo.get_by_name(law_name)
    if not law:
        bundle.evidence_text = f"법령 '{law_name}'을 찾을 수 없습니다."
        return

    articles = await repo.get_articles(law["id"])
    lines = [f"**{law_name}** (총 {len(articles)}조)\n"]
    for a in articles[:40]:
        num = a.get("article_number", "")
        title = a.get("article_title", "")
        content = a.get("content", "")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"**제{num}조 {title}**: {content}")
        bundle.sources.append({"law_name": law_name, "article": str(num)})

    bundle.evidence_text = "\n\n".join(lines)


async def _strategy_keyword_search(cq, pg, session, bundle, progress):
    """키워드 검색."""
    query = cq.expanded
    await progress("법령 검색", query)

    # 법령 + 조문 병렬 검색
    laws_task = pg.search_laws(query, limit=5)
    articles_task = _search_articles_db(session, query, limit=15)
    laws_result, articles_result = await asyncio.gather(laws_task, articles_task)

    laws, _ = laws_result
    lines = []

    if laws:
        lines.append("## 관련 법령")
        for law in laws[:5]:
            name = law.get("name", "")
            cat = law.get("category", "")
            lines.append(f"- **{name}** ({cat})")
            bundle.sources.append({"law_name": name})

    if articles_result:
        lines.append("\n## 관련 조문")
        for a in articles_result[:15]:
            lname = a["law_name"]
            num = a["article_number"]
            title = a.get("article_title", "")
            content = a["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"### {lname} 제{num}조 {title}\n{content}")
            bundle.sources.append({"law_name": lname, "article": str(num)})

    bundle.evidence_text = "\n\n".join(lines) if lines else "검색 결과가 없습니다."


async def _strategy_multi_law(cq, pg, session, bundle, progress):
    """여러 법령 분석."""
    query = cq.expanded
    await progress("법령 검색", query)

    laws, _ = await pg.search_laws(query, limit=8)
    if not laws:
        bundle.evidence_text = "관련 법령을 찾을 수 없습니다."
        return

    top_laws = [law.get("name", "") for law in laws[:5]]
    await progress("조문 검색", f"{len(top_laws)}개 법령")

    # 각 법령에서 관련 조문 병렬 검색
    tasks = []
    for law_name in top_laws:
        tasks.append(_search_articles_db(None, query, law_name=law_name, limit=5))

    results = await asyncio.gather(*tasks)
    lines = []
    for law_name, articles in zip(top_laws, results):
        if articles:
            lines.append(f"## {law_name}")
            for a in articles:
                num = a["article_number"]
                title = a.get("article_title", "")
                content = a["content"]
                if len(content) > 200:
                    content = content[:200] + "..."
                lines.append(f"**제{num}조 {title}**: {content}")
                bundle.sources.append({"law_name": law_name, "article": str(num)})

    bundle.evidence_text = "\n\n".join(lines) if lines else "관련 조문을 찾을 수 없습니다."


async def _strategy_thematic_deep(cq, repo, pg, session, bundle, progress):
    """심층 주제 분석 — 핵심: 여러 키워드로 병렬 검색."""
    # 대상 법령 결정
    if cq.law_names:
        target_laws = cq.law_names
    else:
        # 쿼리에서 법령 검색
        await progress("법령 검색", cq.expanded)
        laws, _ = await pg.search_laws(cq.expanded, limit=5)
        target_laws = [law.get("name", "") for law in laws[:3]]

    if not target_laws:
        bundle.evidence_text = "관련 법령을 찾을 수 없습니다."
        return

    await progress("심층 분석", f"{len(target_laws)}개 법령 × {len(cq.analysis_keywords)}개 키워드")

    # 모든 (법령, 키워드) 조합으로 병렬 검색
    tasks = []
    task_meta = []  # (law_name, keyword) tracking
    for law_name in target_laws:
        for kw in cq.analysis_keywords:
            tasks.append(_search_articles_db(None, kw, law_name=law_name, limit=5))
            task_meta.append((law_name, kw))

    results = await asyncio.gather(*tasks)

    # 결과 병합 (법령별 → 중복 제거)
    by_law: dict[str, dict[str, dict]] = {}  # law -> article_key -> article
    for (law_name, kw), articles in zip(task_meta, results):
        if not articles:
            continue
        if law_name not in by_law:
            by_law[law_name] = {}
        for a in articles:
            key = f"{a['article_number']}"
            if key not in by_law[law_name]:
                by_law[law_name][key] = a

    lines = []
    for law_name, articles_map in by_law.items():
        sorted_arts = sorted(articles_map.values(), key=lambda x: int(x.get("article_number", 0) or 0))
        if sorted_arts:
            lines.append(f"## {law_name} ({len(sorted_arts)}개 조문)")
            for a in sorted_arts:
                num = a["article_number"]
                title = a.get("article_title", "")
                content = a["content"]
                if len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"### 제{num}조 {title}\n{content}")
                bundle.sources.append({"law_name": law_name, "article": str(num)})

    # 분석 템플릿 매칭 → 남한법 비교 필요 시 추가 조회
    template = _match_analysis_template(cq.original)
    if template and template.get("requires_kr_law") and target_laws:
        from app.compare.beopmang_client import KrLawClient
        # 첫 번째 법령의 매핑된 남한법 조회
        mapping = await get_mapping(session, target_laws[0])
        kr_names = mapping.get("kr_names", []) if "error" not in mapping else []
        if kr_names:
            kr_name = kr_names[0]
            await progress("남한법 조회", kr_name)
            client = KrLawClient()
            try:
                kr_articles = await client.get_article_by_number(kr_name)
                if kr_articles:
                    lines.append(f"\n## 남한 대응법: {kr_name} ({len(kr_articles)}조)")
                    for a in kr_articles[:10]:
                        num = a.get("article_number", "")
                        title = a.get("article_title", "")
                        content = a.get("content", "")
                        if len(content) > 200:
                            content = content[:200] + "..."
                        lines.append(f"**{num} {title}**: {content}")
                        bundle.sources.append({"law_name": kr_name, "article": num})
            except Exception:
                lines.append(f"\n남한법 '{kr_name}' 조회 불가")
            finally:
                await client.close()

    # 분석 프레임워크 주입
    if template and template.get("analysis_framework"):
        bundle.analysis_framework = template["analysis_framework"]

    bundle.evidence_text = "\n\n".join(lines) if lines else "관련 조문을 찾을 수 없습니다."


async def _strategy_comparison(cq, repo, pg, session, bundle, progress):
    """남북법 비교 — 양쪽 조문 나란히 인용."""
    from app.compare.beopmang_client import KrLawClient

    law_name = cq.law_names[0] if cq.law_names else ""
    if not law_name:
        await progress("법령 검색", cq.expanded)
        laws, _ = await pg.search_laws(cq.expanded, limit=1)
        if laws:
            law_name = laws[0].get("name", "")

    if not law_name:
        bundle.evidence_text = "비교할 법령을 특정할 수 없습니다."
        return

    await progress("남북법 비교", law_name)
    mapping = await get_mapping(session, law_name)

    lines = [f"## {law_name} 남북법 비교"]
    kr_names: list[str] = []
    if "error" not in mapping:
        kr_names = mapping.get("kr_names", [])
        lines.append(f"- 대응 남한법: {', '.join(kr_names)}")
        lines.append(f"- 관계: {mapping.get('relationship', '')}")
        if mapping.get("overlap_areas"):
            lines.append(f"- 공통 영역: {', '.join(mapping['overlap_areas'])}")
        bundle.sources.append({"law_name": law_name, "type": "compare"})

    # 북한법 주요 조문 조회
    law = await repo.get_by_name(law_name)
    if law:
        articles = await repo.get_articles(law["id"])
        lines.append(f"\n## 북한: {law_name} ({len(articles)}조)")
        for a in articles[:15]:
            num = a.get("article_number", "")
            title = a.get("article_title", "")
            content = a.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"**제{num}조 {title}**: {content}")
            bundle.sources.append({"law_name": law_name, "article": str(num)})

    # 남한법 조문 조회 (법제처 API)
    if kr_names:
        kr_name = kr_names[0]
        await progress("남한법 조회", kr_name)
        client = KrLawClient()
        try:
            kr_articles = await client.get_article_by_number(kr_name)
            if kr_articles:
                lines.append(f"\n## 남한: {kr_name} ({len(kr_articles)}조)")
                for a in kr_articles[:15]:
                    num = a.get("article_number", "")
                    title = a.get("article_title", "")
                    content = a.get("content", "")
                    if len(content) > 200:
                        content = content[:200] + "..."
                    lines.append(f"**{num} {title}**: {content}")
                    bundle.sources.append({"law_name": kr_name, "article": num})
            else:
                lines.append(f"\n남한법 '{kr_name}' 조문 조회 불가 (법제처 API 오류)")
        except Exception:
            lines.append(f"\n남한법 '{kr_name}' 조문 조회 불가 (법제처 API 오류)")
        finally:
            await client.close()
    # 분석 프레임워크 매칭
    template = _match_analysis_template(cq.original)
    if template and template.get("analysis_framework"):
        bundle.analysis_framework = template["analysis_framework"]

    bundle.evidence_text = "\n\n".join(lines)


async def _strategy_term_lookup(cq, session, pg, bundle, progress):
    """용어 조회."""
    term = " ".join(cq.keywords) if cq.keywords else cq.original
    await progress("용어 조회", term)

    result = await session.execute(
        text("SELECT * FROM compare_terms WHERE kp_term ILIKE :t OR kr_term ILIKE :t LIMIT 10"),
        {"t": f"%{term}%"},
    )
    rows = result.mappings().all()

    lines = ["## 용어 대조"]
    if rows:
        for r in rows:
            kp = r.get("kp_term", "")
            kr = r.get("kr_term", "")
            cat = r.get("category", "")
            lines.append(f"- **{kp}** (북) ↔ **{kr}** (남) [{cat}]")
    else:
        lines.append(f"'{term}'에 대한 용어 매핑이 없습니다.")

    # 조문에서 사용 사례도 검색
    articles = await _search_articles_db(session, term, limit=5)
    if articles:
        lines.append("\n## 법령에서의 사용례")
        for a in articles:
            lname = a["law_name"]
            num = a["article_number"]
            content = a["content"][:150] + "..."
            lines.append(f"- {lname} 제{num}조: {content}")
            bundle.sources.append({"law_name": lname, "article": str(num)})

    bundle.evidence_text = "\n\n".join(lines)


# ── 공통 DB 검색 ──

async def _search_articles_db(
    session_or_none,
    query: str,
    law_name: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """조문 내용 ILIKE 검색. session=None이면 새 세션 생성 (병렬 안전)."""
    if session_or_none is None:
        factory = get_session_factory()
        async with factory() as session:
            return await _search_articles_impl(session, query, law_name, limit)
    return await _search_articles_impl(session_or_none, query, law_name, limit)


async def _search_articles_impl(
    session: AsyncSession,
    query: str,
    law_name: str | None = None,
    limit: int = 10,
) -> list[dict]:
    expanded = expand_query(query)
    words = expanded.split()
    if not words:
        return []

    conditions = " AND ".join(
        f"(a.content ILIKE :w{i} OR a.article_title ILIKE :w{i})" for i in range(len(words))
    )
    params: dict = {}
    for i, w in enumerate(words):
        params[f"w{i}"] = f"%{w}%"

    law_filter = ""
    if law_name:
        law_filter = "AND l.name = :law_name"
        params["law_name"] = law_name

    sql = f"""
        SELECT a.article_number, a.article_title, a.content, l.name as law_name
        FROM articles a JOIN laws l ON a.law_id = l.id
        WHERE ({conditions}) {law_filter}
        ORDER BY l.name, a.position
        LIMIT :lim
    """
    params["lim"] = limit
    result = await session.execute(text(sql), params)
    rows = result.mappings().all()

    if not rows and len(words) > 1:
        or_conditions = " OR ".join(
            f"(a.content ILIKE :w{i} OR a.article_title ILIKE :w{i})" for i in range(len(words))
        )
        sql_or = f"""
            SELECT a.article_number, a.article_title, a.content, l.name as law_name
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE ({or_conditions}) {law_filter}
            ORDER BY l.name, a.position
            LIMIT :lim
        """
        result = await session.execute(text(sql_or), params)
        rows = result.mappings().all()

    return [dict(r) for r in rows]


# ── 프롬프트 빌더 ──

SYNTHESIS_SYSTEM_PROMPT = """당신은 북한법 전문 분석가입니다. 아래 제공된 조사 결과만을 바탕으로 사용자 질문에 답변하세요.

규칙:
1. 제공된 조문 내용만 사용하세요. 추측하거나 일반 지식으로 답변하지 마세요.
2. 모든 주장에 반드시 법령명과 조문번호를 인용하세요.
3. 조문 원문을 인용하고, 사용자가 이해하기 쉽게 분석을 추가하세요.
4. 마크다운으로 체계적으로 작성하세요 (제목, 목록, 강조 활용).
5. 한국어로 답변하세요.
6. 중간 과정이나 계획을 출력하지 마세요. 바로 최종 답변만 작성하세요."""


def build_synthesis_prompt(
    user_query: str,
    bundle: EvidenceBundle,
    history: list[dict] | None = None,
) -> str:
    """LLM에게 전달할 종합 프롬프트 구성 (대화 이력 포함)."""
    evidence = bundle.evidence_text
    if len(evidence) > 8000:
        evidence = evidence[:8000] + "\n\n...(일부 생략)"

    # 대화 이력 요약 (최근 3턴)
    history_section = ""
    if history:
        recent = history[-6:]  # 최근 3턴
        hist_lines = []
        for msg in recent:
            role = "사용자" if msg["role"] == "user" else "AI"
            content = msg["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            hist_lines.append(f"[{role}]: {content}")
        if hist_lines:
            history_section = "## 이전 대화\n" + "\n".join(hist_lines) + "\n\n"

    framework_section = ""
    if bundle.analysis_framework:
        framework_section = f"\n## 분석 프레임워크\n{bundle.analysis_framework}\n"

    return f"""{history_section}사용자 질문: {user_query}

## 조사 결과

{evidence}
{framework_section}
---
위 조사 결과를 바탕으로 사용자 질문에 체계적이고 구체적으로 답변하세요.
{f'위 분석 프레임워크의 각 항목별로 구조화하여 답변하세요.' if bundle.analysis_framework else ''}
이전 대화 문맥이 있다면 그것을 참고하여 연속적으로 답변하세요.
반드시 법령명과 조문번호를 인용하세요."""


# ── 인용 검증 ──

_CITATION_PAT = re.compile(r"([\w가-힣,\s]+?(?:법|령))\s*제(\d+)조")


async def validate_citations(
    response_text: str,
    session: AsyncSession,
) -> dict:
    """LLM 답변에서 '법령명 제N조' 패턴을 추출하고 실존 확인.

    Returns: {"valid": [...], "invalid": [...], "unchecked": [...]}
    """
    citations = _CITATION_PAT.findall(response_text)
    if not citations:
        return {"valid": [], "invalid": [], "unchecked": []}

    law_names = await _get_law_names(session)
    valid = []
    invalid = []
    unchecked = []

    # 북한법 검증 (DB)
    repo = LawRepository(session)
    for raw_name, art_num in citations:
        name = raw_name.strip()
        citation_str = f"{name} 제{art_num}조"

        if name in law_names:
            # 북한법 — DB에서 확인
            law = await repo.get_by_name(name)
            if law:
                articles = await repo.get_articles(law["id"], article_number=art_num)
                if articles:
                    valid.append(citation_str)
                else:
                    invalid.append(citation_str)
            else:
                invalid.append(citation_str)
        else:
            # 남한법 또는 미확인 — unchecked로 분류
            unchecked.append(citation_str)

    return {"valid": valid, "invalid": invalid, "unchecked": unchecked}


def build_fallback_answer(user_query: str, bundle: EvidenceBundle) -> str:
    """LLM이 빈 응답을 반환할 경우 증거에서 직접 답변 생성."""
    if not bundle.evidence_text or bundle.evidence_text == "검색 결과가 없습니다.":
        return "관련 법령을 찾을 수 없습니다. 다른 키워드로 질문해 주세요."

    lines = [f"**'{user_query}'에 대한 조사 결과입니다.**\n"]
    lines.append(bundle.evidence_text)

    if bundle.sources:
        lines.append("\n---\n**출처:**")
        seen = set()
        for s in bundle.sources:
            key = (s.get("law_name", ""), s.get("article", ""))
            if key not in seen:
                seen.add(key)
                name = s.get("law_name", "")
                art = s.get("article", "")
                if art:
                    lines.append(f"- {name} 제{art}조")
                elif name:
                    lines.append(f"- {name}")

    return "\n".join(lines)
