import pytest


@pytest.fixture
def sample_frontmatter() -> dict:
    return {
        "title": "조선민주주의인민공화국 헌법",
        "law_id": "kp-constitution-2019",
        "enacted": "1948-09-08",
        "amended": "2019-08-29",
        "category": "헌법",
    }


@pytest.fixture
def sample_md_content() -> str:
    return """\
---
title: 조선민주주의인민공화국 헌법
law_id: kp-constitution-2019
enacted: "1948-09-08"
amended: "2019-08-29"
category: 헌법
---

## 제1조

조선민주주의인민공화국은 전체 조선인민의 리익을 대표하는 자주적인 사회주의국가이다.

## 제2조

조선민주주의인민공화국은 혁명과 건설의 력사적 성과에 토대하여 세워진 국가이다.

## 제3조

조선민주주의인민공화국은 맑스-레닌주의를 우리 나라의 현실에 창조적으로 적용한 조선로동당의 주체사상을 자기 활동의 지도적지침으로 삼는다.
"""
