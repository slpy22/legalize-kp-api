"""T8 — 논문 팀 7차 제보 #70·#71·#72·#74 (적재기·API). DB 없이 파서·순수 함수·스텁으로 검증."""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from import_laws import parse_markdown_text  # noqa: E402
from import_law_versions import version_action  # noqa: E402
from app.repositories import law_repo as lr  # noqa: E402
from app.services import law_service as ls  # noqa: E402


# ---------------------------------------------------------------- #70 제목 속 괄호
def test_article_title_keeps_one_nested_paren():
    md = ("---\n제목: 공증법\n---\n\n"
          "##### 제15조 (시(구역), 군공증기관의 관할)\n시(구역), 군공증기관은 …\n"
          "##### 제16조 (도(직할시)공증기관의 관할)\n도(직할시)공증기관은 …\n"
          "##### 제17조 (목적)\n본문\n"
          "##### 제18조\n제목 없는 조\n"
          "##### 제29조 (출생증, 공민증(평양시민증)발급대상)\n본문29\n")
    arts = {a["article_number"]: a for a in parse_markdown_text(md)["articles"]}
    assert arts["15"]["article_title"] == "시(구역), 군공증기관의 관할"
    assert arts["16"]["article_title"] == "도(직할시)공증기관의 관할"
    assert arts["16"]["content"] == "도(직할시)공증기관은 …"       # 잘린 꼬리가 본문에 들어가지 않는다
    assert arts["17"]["article_title"] == "목적"                   # 괄호 없는 제목은 그대로
    assert arts["18"]["article_title"] == "" and arts["18"]["content"] == "제목 없는 조"
    assert arts["29"]["article_title"] == "출생증, 공민증(평양시민증)발급대상"


def test_fullwidth_paren_title_still_parsed():
    md = "---\n제목: 법\n---\n\n##### 제1조 （도（직할시）의 관할）\n본문\n"
    a = parse_markdown_text(md)["articles"][0]
    assert a["article_title"] == "도（직할시）의 관할" and a["content"] == "본문"


# ---------------------------------------------------------------- #71 항목 제목
def test_item_heading_is_always_the_title():
    long = "온 사회를 김일성-김정일주의화하기 위하여 몸바쳐 투쟁하여야 한다."
    md = (f"---\n제목: 당의유일적령도체계확립의 10대 원칙\n---\n\n"
          f"##### 1. {long}\n첫째 원칙 본문.\n"
          f"##### 2. 핵무력의 사명\n둘째 본문\n"
          f"##### 3. 위대한 김일성동지와 김정일동지를 따라배워 고상한 정신도덕적 풍모와 혁명적사업방법,\n"
          f"인민적사업작풍을 지녀야 한다.\n")
    arts = {a["article_number"]: a for a in parse_markdown_text(md)["articles"]}
    assert list(arts) == ["항1", "항2", "항3"]
    assert arts["항1"]["article_title"] == long and arts["항1"]["content"] == "첫째 원칙 본문."
    assert arts["항2"]["article_title"] == "핵무력의 사명" and arts["항2"]["content"] == "둘째 본문"
    # 줄바꿈으로 끊긴 머리글도 제목(머리글 줄)과 본문(이어지는 줄)으로 — 제목이 비지 않는다
    assert arts["항3"]["article_title"].endswith("혁명적사업방법,")
    assert arts["항3"]["content"] == "인민적사업작풍을 지녀야 한다."
    assert all(a["kind"] == "항목" for a in arts.values())


# ---------------------------------------------------------------- #72 versions.action
def test_version_action_follows_frontmatter_history_then_adoption_date():
    fm = {"채택일": "2003-08-27",
          "개정이력": [{"일자": "2003-08-27", "내용": "채택"}, {"일자": "2022-05-31", "내용": "수정보충"}]}
    assert version_action(fm, "2003-08-27", "수정보충") == "채택"       # dump 기본값을 덮는다
    assert version_action(fm, "2022-05-31", "수정보충") == "수정보충"
    # 개정이력에 없지만 채택일과 같은 판본(개성공업지구법 2002-11-20 등 8판본) → 채택
    assert version_action({"채택일": "2002-11-20", "개정이력": []}, "2002-11-20", "수정보충") == "채택"
    # 근거가 전혀 없으면 dump 값 유지(10대 원칙·당규약처럼 채택일 없는 문서)
    assert version_action({"개정이력": []}, "2013-06-19", "수정보충") == "수정보충"
    assert version_action({}, "2013-06-19", None) is None
    # 일자가 date 객체(yaml 미인용)로 와도 문자열 비교로 맞는다
    import datetime as dt
    fm2 = {"개정이력": [{"일자": dt.date(2021, 11, 30), "내용": "채택"}]}
    assert version_action(fm2, "2021-11-30", "수정보충") == "채택"


# ---------------------------------------------------------------- #74 resolve short
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Sess:
    """resolve_name 이 치는 SQL 을 순서대로 흉내낸다: exact → alias → short LIKE."""

    def __init__(self, exact=None, alias=None, short=()):
        self._exact, self._alias, self._short = exact, alias, list(short)
        self.sql: list[str] = []

    async def execute(self, stmt, params=None):
        q = str(stmt)
        self.sql.append(q)
        if "name = :name" in q:
            return _Res([self._exact] if self._exact else [])
        if "@> CAST(:n AS jsonb)" in q:
            return _Res([self._alias] if self._alias else [])
        if "LIKE :pat" in q:
            return _Res(self._short)
        raise AssertionError(q)


def test_resolve_short_match_is_candidate_only_with_hint():
    repo = lr.LawRepository(_Sess(short=[{"id": 7, "name": "사회보험 및 사회보장법"}]))
    out = asyncio.run(repo.resolve_name("사회보장법"))
    assert out["resolved"] is None and out["law_id"] is None and out["matched_by"] is None
    assert out["candidates"] == [{"id": 7, "name": "사회보험 및 사회보장법", "matched_by": "short"}]
    assert out["hint"] and "자동 해석하지 않았" in out["hint"]


def test_resolve_abbreviation_from_master_list_is_alias_match():
    # 마스터목록 abbreviations → frontmatter->'abbreviations' 로 들어온 '헌법' 은 alias 단계에서 해석
    sess = _Sess(alias={"id": 1, "name": "조선민주주의인민공화국 헌법"})
    repo = lr.LawRepository(sess)
    out = asyncio.run(repo.resolve_name("헌법"))
    assert out["resolved"] == "조선민주주의인민공화국 헌법" and out["matched_by"] == "alias"
    assert out["candidates"] == [] and out["hint"] is None
    assert any("frontmatter->'abbreviations'" in q for q in sess.sql)


def test_resolve_exact_and_none_keep_shape():
    out = asyncio.run(lr.LawRepository(_Sess(exact={"id": 3, "name": "형법"})).resolve_name("형법"))
    assert out["matched_by"] == "exact" and out["hint"] is None
    out = asyncio.run(lr.LawRepository(_Sess()).resolve_name("없는법"))
    assert out == {"input": "없는법", "resolved": None, "law_id": None,
                   "matched_by": None, "candidates": [], "hint": None}


def test_get_name_warning_only_for_short_fallback(monkeypatch):
    monkeypatch.setattr(ls, "get_config", lambda: {"sources": {}})
    law = {"name": "사회보험 및 사회보장법", "frontmatter": {"name_alias": ["사보법"]}}
    aliases = ["사보법"]
    assert ls.name_match_warning("사회보험 및 사회보장법", law, aliases) is None
    assert ls.name_match_warning("사보법", law, aliases) is None
    w = ls.name_match_warning("사회보장법", law, aliases)
    assert w and "사회보장법" in w and "별개 법령명일 수 있" in w
