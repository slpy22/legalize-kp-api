"""T7 §9-③ — 남북 비교 매핑 미작성은 오류가 아닌 정상 상태(mapped=False)로 돌려준다."""
from __future__ import annotations

import asyncio

from app.compare import mapping_service as ms


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _StubSession:
    """get_mapping 이 쓰는 execute() 만 흉내낸다."""

    def __init__(self, row):
        self._row = row
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _Result(self._row)


def test_get_mapping_not_found_is_normal_state_not_error():
    sess = _StubSession(row=None)
    out = asyncio.run(ms.get_mapping(sess, "관광법"))
    assert "error" not in out
    assert out["mapped"] is False and out["status"] == "no_mapping"
    assert out["kp_name"] == "관광법"
    assert "아직 작성되지 않았습니다" in out["message"]
    # 소비처가 바로 쓸 수 있게 배열 필드는 빈 리스트
    for k in ("kr_names", "kr_categories", "overlap_areas", "kp_unique", "kr_unique", "article_mappings"):
        assert out[k] == []
    assert sess.calls and sess.calls[0][1] == {"kp_name": "관광법"}


def test_get_mapping_found_marks_mapped_true():
    row = {
        "id": 1, "kp_name": "민법", "kp_category": "형·민사",
        "kr_names": ["민법"], "kr_categories": None, "overlap_areas": None,
        "kp_unique": None, "kr_unique": None, "relationship": "equivalent",
        "confidence": "high", "source": "ai", "notes": None, "article_mappings": None,
    }
    out = asyncio.run(ms.get_mapping(_StubSession(row=row), "민법"))
    assert out["mapped"] is True and out["status"] == "mapped"
    assert out["kr_names"] == ["민법"]
    assert out["kr_categories"] == [] and out["kp_unique"] == []   # None 배열은 [] 로
