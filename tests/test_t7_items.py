"""T7 추가 — 조문 번호가 없는 법령의 '##### N. 머리글' 항목을 조문으로 받는다(키 '항N')."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from import_laws import parse_markdown_text  # noqa: E402

LONG = "조선민주주의인민공화국의 핵무기는 우리 공화국에 대한 미국의 지속적으로 가증되는 적대시정책과 핵위협에 대처하여 부득이하게 갖추게 된 정당한 방위수단이다."

ITEM_LAW = f"""---
제목: 자위적핵보유국의 지위를 더욱 공고히 할데 대하여
출처: mobu
---

##### 1. {LONG}
##### 2. 핵무력의 사명
핵무력은 침략과 공격을 억제한다.
억제가 실패하면 격퇴한다.
##### 10. 해당 기관들은 이 법령을 집행하기 위한 실무적대책을 철저히 세울것이다.
"""


def test_item_style_law_is_parsed_with_hang_keys():
    arts = parse_markdown_text(ITEM_LAW)["articles"]
    assert [a["article_number"] for a in arts] == ["항1", "항2", "항10"]
    assert all(a["kind"] == "항목" for a in arts)
    assert all(a["part"] is None and a["chapter"] is None and a["section"] is None for a in arts)
    assert all(a["supplement"] is None for a in arts)


def test_item_head_is_title_regardless_of_length():
    """T8 #71: 머리글은 길이·문장 여부와 무관하게 article_title(T7 의 길이 휴리스틱 폐기)."""
    arts = {a["article_number"]: a for a in parse_markdown_text(ITEM_LAW)["articles"]}
    a1 = arts["항1"]
    assert a1["article_title"] == LONG and a1["content"] == ""
    a2 = arts["항2"]
    assert a2["article_title"] == "핵무력의 사명"
    assert a2["content"] == "핵무력은 침략과 공격을 억제한다.\n억제가 실패하면 격퇴한다."
    a10 = arts["항10"]
    assert a10["article_title"].startswith("해당 기관들은") and a10["content"] == ""


def test_item_headings_ignored_when_law_has_real_articles():
    md = "---\n제목: 보통법\n---\n\n##### 제1조 (목적)\n본문\n##### 1. 이것은 조문 안의 번호매김이 아니라 h5 잡음\n##### 제2조\n본문2\n"
    arts = parse_markdown_text(md)["articles"]
    assert [a["article_number"] for a in arts] == ["1", "2"]   # 제N조가 있으면 항목 머리는 무시
    assert all(a["kind"] == "조" for a in arts)


def test_kind_marks_supplement_articles():
    md = "---\n제목: 법\n---\n\n##### 제1조\n가\n## 부칙\n##### 제1조\n나\n"
    arts = parse_markdown_text(md)["articles"]
    assert [(a["article_number"], a["kind"]) for a in arts] == [("1", "조"), ("부칙1", "부칙")]
