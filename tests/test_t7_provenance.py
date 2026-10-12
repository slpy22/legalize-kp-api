"""T7 — 출처 표시(provenance)와 law_history 의 이력추가(원문 미확보) 병합.
DB 없이: provenance/_flag 는 순수 함수, history 는 스텁 repo 로 검증한다."""
from __future__ import annotations

import asyncio

import pytest

from app.services import law_service as ls

SOURCES = {
    "nis": {"name": "국가정보원", "label": "국정원 법령집", "desc": "2024 법령집"},
    "mobu": {"name": "법무부", "label": "법무부 통일법제DB", "desc": "unilaw"},
    "nktechlab": {"name": "NK TechLab", "label": "NK TechLab", "desc": "앱 OCR", "unverified": True},
}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(ls, "get_config", lambda: {"sources": SOURCES})


def test_flag_parses_bool_and_strings():
    assert ls._flag(True) is True and ls._flag(False) is False
    assert ls._flag("true") is True and ls._flag("False") is False
    assert ls._flag(None) is None


def test_provenance_labels_source_and_ocr_only():
    p = ls.provenance("nis", {"OCR여부": False, "정본여부": True})
    assert p["label"] == "국정원 법령집" and p["labels"] == ["국정원 법령집"]
    assert p["is_ocr"] is False and p["nis_edition"] is True

    p = ls.provenance("nktechlab", {"OCR여부": True, "정본여부": False})
    assert p["labels"] == ["NK TechLab", "미검증 OCR"]
    assert p["source_desc"]["unverified"] is True

    # 법무부본은 정본여부 false 지만 '비정본' 같은 라벨을 붙이지 않는다(수집 합의)
    p = ls.provenance("mobu", {"정본여부": False})
    assert p["labels"] == ["법무부 통일법제DB"]
    assert all("비정본" not in lbl for lbl in p["labels"])

    # 정본여부가 없으면 '해당 없음'(None); 모르는 출처는 코드가 라벨
    p = ls.provenance("zzz", {})
    assert p["nis_edition"] is None and p["label"] == "zzz" and p["labels"] == ["zzz"]

    # 명시 인자가 frontmatter 보다 우선 (laws.is_ocr 컬럼 경로)
    assert ls.provenance("nis", {"OCR여부": False}, is_ocr=True)["is_ocr"] is True


def test_provenance_excerpt_label():
    """관리 결정 ②: 수집 T6 가 넣는 frontmatter '발췌본: true' → labels 에 '발췌본' (출처 라벨 다음, OCR 앞)."""
    p = ls.provenance("nis", {"발췌본": True})
    assert p["excerpt"] is True and p["labels"] == ["국정원 법령집", "발췌본"]
    p = ls.provenance("nktechlab", {"발췌본": "true", "OCR여부": True})
    assert p["labels"] == ["NK TechLab", "발췌본", "미검증 OCR"]
    # 없으면 False, 라벨 없음 (기존 응답 불변)
    p = ls.provenance("mobu", {})
    assert p["excerpt"] is False and p["labels"] == ["법무부 통일법제DB"]
    # 명시 인자 경로(list_versions/search 의 excerpt_flag 스칼라)
    assert ls.provenance("nis", None, excerpt=ls._flag("true"))["labels"][-1] == "발췌본"


class _StubRepo:
    """history() 가 쓰는 세 메서드만 흉내낸다."""

    def __init__(self, law, amendments, versions):
        self._law, self._am, self._vs = law, amendments, versions

    async def get_by_name(self, name):
        return self._law

    async def get_amendments(self, law_id):
        return self._am

    async def list_versions(self, law_id):
        return self._vs


def test_history_merges_unverified_and_month_only_entries():
    law = {
        "id": 7, "name": "하천법", "source": "nis", "is_ocr": False,
        "frontmatter": {
            "정본여부": True,
            "개정이력": [
                {"일자": "2020-01-01", "내용": "채택"},
                # 월만 아는 이력추가 — DATE 로 적재되지 않아 amendments 에 없다
                {"일자": "2023-10", "내용": "수정보충", "원문미확보": True,
                 "외부근거": "김일성종합대학학보 2026년 1호, doc_id abc", "비고": "법령 제26호"},
                # ISO 일자 이력추가 — import 가 amendments 에도 넣는다
                {"일자": "2024-10-07", "내용": "수정보충", "원문미확보": True, "외부근거": "학보 2025-3"},
            ],
        },
    }
    amendments = [
        {"id": 1, "date": "2020-01-01", "action": "채택", "basis": None, "effective_date": None},
        {"id": 2, "date": "2024-10-07", "action": "수정보충", "basis": None, "effective_date": None},
    ]
    versions = [{"id": 11, "law_id": 7, "version_date": "2020-01-01", "action": "채택",
                 "source": "nis", "ocr_flag": "false", "auth_flag": "true"}]

    svc = ls.LawService(_StubRepo(law, amendments, versions), search_engine=None)
    out = asyncio.run(svc.history("하천법"))

    rows = {r["date"]: r for r in out["amendments"]}
    assert [r["date"] for r in out["amendments"]] == ["2020-01-01", "2023-10", "2024-10-07"]

    orig = rows["2020-01-01"]
    assert orig["has_text"] is True and orig["unverified"] is False
    assert orig["provenance"]["label"] == "국정원 법령집" and orig["provenance"]["is_ocr"] is False

    month = rows["2023-10"]            # frontmatter 에서만 온 행
    assert month["has_text"] is False and month["unverified"] is True
    assert month["external_basis"].startswith("김일성종합대학학보")
    assert month["note"] == "법령 제26호" and month["provenance"] is None
    assert str(month["id"]).startswith("a")

    iso = rows["2024-10-07"]           # amendments 에 있던 행에 플래그만 덧붙음
    assert iso["id"] == 2 and iso["unverified"] is True and iso["has_text"] is False
    assert iso["external_basis"] == "학보 2025-3"

    assert out["provenance"]["labels"] == ["국정원 법령집"]
    assert out["date_disputes"] == []


def test_history_handles_law_without_history_keys():
    law = {"id": 1, "name": "단순법", "source": "mobu", "is_ocr": False, "frontmatter": {}}
    svc = ls.LawService(_StubRepo(law, [], []), search_engine=None)
    out = asyncio.run(svc.history("단순법"))
    assert out["amendments"] == []
    assert out["provenance"]["nis_edition"] is None
