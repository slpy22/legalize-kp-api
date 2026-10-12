"""T4 — 저장소 markdown 파서: 편(part)/장(chapter)/절(section) 추적, 부칙/서문 블록,
관(####) 경계, 시행일·날짜이견 frontmatter 통과. DB 없이 순수 함수만 검증한다."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from import_laws import parse_markdown_text  # noqa: E402

SAMPLE = """---
제목: 테스트법
출처: nis
개정이력:
- 일자: '2020-01-01'
  내용: 채택
- 일자: '2022-05-05'
  내용: 수정보충
  시행일: '2022-06-01'
날짜이견:
- 항목: 채택일
  원문값: '2020-01-01'
  이견값: '2020-02-01'
  출처: 학보 2025-3
  비고: 메모
---

# 제1편 총칙
## 제1장 기본
##### 제1조 (목적)
본문1
##### 제2조
본문2
## 제2장 권리
### 제1절 일반
##### 제3조 (권리)
본문3
#### 제1관 세부
##### 제4조
본문4
# 제2편 각칙
## 제1장 재산
##### 제5조
본문5
## 부칙
##### 제1조
부칙본문
"""


def _by_pos(parsed):
    return {a["position"]: a for a in parsed["articles"]}


def test_part_chapter_section_tracking():
    arts = _by_pos(parse_markdown_text(SAMPLE))
    assert len(arts) == 6
    a1 = arts[0]
    assert a1["article_number"] == "1"
    assert a1["article_title"] == "목적"
    assert a1["part"] == "제1편 총칙"
    assert a1["chapter"] == "제1장 기본"
    assert a1["section"] is None
    assert a1["content"] == "본문1"
    a3 = arts[2]
    assert a3["chapter"] == "제2장 권리"
    assert a3["section"] == "제1절 일반"


def test_heading_boundaries_do_not_bleed_into_content():
    arts = _by_pos(parse_markdown_text(SAMPLE))
    # '## 제2장' 헤더가 제2조 본문에 섞이지 않는다
    assert arts[1]["content"] == "본문2"
    # '#### 제1관' 헤더가 제3조 본문에 섞이지 않는다 (관은 경계만, 컬럼 없음)
    assert arts[2]["content"] == "본문3"
    assert arts[3]["content"] == "본문4"


def test_chapter_restarts_per_part_are_disambiguated_by_part():
    arts = _by_pos(parse_markdown_text(SAMPLE))
    a5 = arts[4]
    assert a5["part"] == "제2편 각칙"
    assert a5["chapter"] == "제1장 재산"  # 제1장 이 다시 시작하지만 part 가 다르다
    # 이전 편의 절이 새 편으로 넘어오지 않는다
    assert a5["section"] is None
    assert arts[0]["chapter"] != a5["chapter"] or arts[0]["part"] != a5["part"]


def test_supplement_articles_are_rekeyed_and_labeled():
    parsed = parse_markdown_text(SAMPLE)
    arts = _by_pos(parsed)
    sup = arts[5]
    assert sup["supplement"] == "부칙"
    assert sup["article_number"] == "부칙1"  # 본칙 제1조와 키가 겹치지 않는다
    assert sup["chapter"] == "부칙"
    assert sup["part"] is None and sup["section"] is None
    assert sup["content"] == "부칙본문"
    keys = [a["article_number"] for a in parsed["articles"]]
    assert len(keys) == len(set(keys))
    # 본칙 조문은 supplement 가 None
    assert all(a["supplement"] is None for p, a in arts.items() if p < 5)


def test_frontmatter_passthrough_effective_date_and_date_disputes():
    fm = parse_markdown_text(SAMPLE)["frontmatter"]
    hist = fm["개정이력"]
    assert hist[0].get("시행일") is None
    assert hist[1]["시행일"] == "2022-06-01"
    dd = fm["날짜이견"]
    assert len(dd) == 1
    assert set(dd[0]) == {"항목", "원문값", "이견값", "출처", "비고"}
    assert dd[0]["항목"] == "채택일" and dd[0]["이견값"] == "2020-02-01"


def test_law_without_structure_has_null_fields():
    txt = "---\n제목: 단순법\n---\n\n##### 제1조\n가\n##### 제2조\n나\n"
    arts = parse_markdown_text(txt)["articles"]
    assert len(arts) == 2
    assert all(a["part"] is None and a["chapter"] is None and a["section"] is None for a in arts)
    assert [a["content"] for a in arts] == ["가", "나"]
