"""T7-b — 발췌본 판본: 발췌조문 노출과 판본 비교(diff_text) 경고. DB 없이 스텁 repo 로 검증."""
from __future__ import annotations

import asyncio

import pytest

from app.services import law_service as ls

SOURCES = {"nis": {"name": "국가정보원", "label": "국정원 법령집", "desc": "d"},
           "mobu": {"name": "법무부", "label": "법무부 통일법제DB", "desc": "d"}}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(ls, "get_config", lambda: {"sources": SOURCES})


def test_provenance_exposes_excerpt_articles():
    fm = {"발췌본": True, "발췌조문": ["제82조", "제86조", "제87조"], "정본여부": True}
    p = ls.provenance("nis", fm)
    assert p["labels"] == ["국정원 법령집", "발췌본"]
    assert p["excerpt_articles"] == ["제82조", "제86조", "제87조"]
    assert p["excerpt_article_numbers"] == ["82", "86", "87"]
    # 발췌본이 아니면 키 자체가 없다(기존 응답 불변)
    assert "excerpt_articles" not in ls.provenance("nis", {"정본여부": True})


def test_excerpt_warning_only_when_an_excerpt_side_exists():
    full = {"version_date": "2013-06-19", "source": "nis", "frontmatter": {}}
    exc = {"version_date": "2014-04-24", "source": "nis",
           "frontmatter": {"발췌본": True, "발췌조문": ["제82조"]}}
    assert ls.excerpt_warning(full, full) is None
    w = ls.excerpt_warning(full, exc)
    assert w and "2014-04-24" in w and "발췌본" in w
    assert "2013-06-19" not in w


class _Repo:
    def __init__(self, law, versions):
        self._law, self._vs = law, versions

    async def get_by_name(self, name):
        return self._law

    async def get_version(self, law_id, d):
        return self._vs.get(d)

    async def list_versions(self, law_id):
        return [{"version_date": k} for k in self._vs]


def test_diff_text_carries_provenance_and_warning():
    law = {"id": 1, "name": "형법", "source": "nis", "frontmatter": {}}
    vs = {
        "2013-06-19": {"version_date": "2013-06-19", "action": "수정보충", "source": "nis",
                       "frontmatter": {"정본여부": True}, "articles": [{"article_number": "1"}], "full_text": "a"},
        "2014-04-24": {"version_date": "2014-04-24", "action": "수정보충", "source": "nis",
                       "frontmatter": {"정본여부": True, "발췌본": True, "발췌조문": ["제82조", "제86조"]},
                       "articles": [{"article_number": "82"}], "full_text": "b"},
    }
    svc = ls.LawService(_Repo(law, vs), search_engine=None)
    out = asyncio.run(svc.diff_text("형법", "2013-06-19", "2014-04-24"))
    assert out["from"]["provenance"]["excerpt"] is False
    assert out["to"]["provenance"]["excerpt"] is True
    assert out["to"]["provenance"]["excerpt_article_numbers"] == ["82", "86"]
    assert out["warning"] and "2014-04-24" in out["warning"]

    out2 = asyncio.run(svc.diff_text("형법", "2013-06-19", "2013-06-19"))
    assert out2["warning"] is None
