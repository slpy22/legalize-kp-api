"""T4 — 출처 설명·별칭·날짜이견 메타, 레거시 이름 해석, 법령명 정정의 API 측 데이터
(law_mappings.json, 웹 301 규칙). DB 접속 없이 검증한다."""
from __future__ import annotations

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY = {"반동문화사상문화배격법": "반동사상문화배격법", "사회주의농총발전법": "사회주의농촌발전법"}


def test_law_mappings_json_has_corrected_names_only():
    d = json.loads((ROOT / "data/compare/law_mappings.json").read_text(encoding="utf-8"))
    names = [m["kp_name"] for m in d["mappings"]]
    for old, new in LEGACY.items():
        assert old not in names, f"옛 오타명 잔존: {old}"
        assert new in names, f"정정명 누락: {new}"
    assert len(names) == len(set(names)), "kp_name 중복 — import 가 ON CONFLICT(kp_name) 라 충돌"


def test_configs_have_sources_and_legacy_names():
    for fn in ("config.yaml", "config.docker.yaml"):
        cfg = yaml.safe_load((ROOT / fn).read_text(encoding="utf-8"))
        assert set(cfg["sources"]) == {"mobu", "nis", "unification", "nktechlab"}, fn  # T7: nktechlab 추가
        for v in cfg["sources"].values():
            assert v.get("name") and v.get("desc") and v.get("label"), fn
        assert cfg["legacy_names"] == LEGACY, fn


def test_source_desc_and_law_meta(monkeypatch):
    from app.services import law_service as ls

    monkeypatch.setattr(
        ls, "get_config",
        lambda: {"sources": {"nis": {"name": "국가정보원", "desc": "2024 법령집"}}},
    )
    assert ls.source_desc("nis") == {"code": "nis", "name": "국가정보원", "desc": "2024 법령집"}
    assert ls.source_desc("zzz") == {"code": "zzz", "name": None, "desc": None}
    assert ls.source_desc(None)["code"] is None

    fm = {"name_alias": ["화페류통법"], "옛이름": ["사회주의헌법"],
          "날짜이견": [{"항목": "채택일", "원문값": "2022-08-07", "이견값": "2022-09-07", "출처": "학보"}]}
    # frontmatter 가 jsonb(dict) 로 오든 문자열로 오든 같아야 한다
    for raw in (fm, json.dumps(fm, ensure_ascii=False)):
        meta = ls.law_meta({"source": "nis", "frontmatter": raw})
        assert meta["aliases"] == ["화페류통법", "사회주의헌법"]
        assert meta["date_disputes"][0]["이견값"] == "2022-09-07"
        assert meta["source_desc"]["name"] == "국가정보원"
    assert ls.law_meta({"source": None, "frontmatter": None})["date_disputes"] == []


def test_canonical_name_uses_legacy_map(monkeypatch):
    import app.core.config as cfgmod
    from app.repositories.law_repo import canonical_name

    monkeypatch.setattr(cfgmod, "get_config", lambda: {"legacy_names": LEGACY})
    assert canonical_name("사회주의농총발전법") == "사회주의농촌발전법"
    assert canonical_name(" 반동문화사상문화배격법 ") == "반동사상문화배격법"
    assert canonical_name("조선민주주의인민공화국 헌법") == "조선민주주의인민공화국 헌법"
    assert canonical_name("") == ""


def test_web_redirects_cover_legacy_urls():
    from urllib.parse import quote

    ts = (ROOT.parent / "legalize-kp-web" / "next.config.ts").read_text(encoding="utf-8")
    assert "async redirects()" in ts
    assert ts.count("statusCode: 301") == 4  # 법령 2 x (본 경로 + :path*)
    for old, new in LEGACY.items():
        assert f'source: "/law/{quote(old)}"' in ts
        assert f'destination: "/law/{quote(new)}"' in ts


def test_external_effective_date_goes_to_date_disputes_not_effective_date(monkeypatch):
    """관리 결정 ③: 외부 자료에서만 확인된 시행일은 '시행일' 키가 아니라
    날짜이견(항목=시행일)으로 들어오고, API 는 그것을 가공 없이 노출하며 effective_date 는 null 로 둔다."""
    import sys
    from app.services import law_service as ls

    sys.path.insert(0, str(ROOT / "scripts"))
    from import_laws import parse_markdown_text

    md = (
        "---\n제목: 형법\n출처: nis\n개정이력:\n- 일자: '2020-11-26'\n  내용: 수정보충\n"
        "날짜이견:\n- 항목: 시행일\n  원문값: '2020-11-26'\n  이견값: '2021-03-01'\n  출처: 'NKLAW 제보 #29'\n"
        "---\n\n##### 제1조\n본문\n"
    )
    fm = parse_markdown_text(md)["frontmatter"]
    assert "시행일" not in fm["개정이력"][0]          # import → effective_date NULL
    monkeypatch.setattr(ls, "get_config", lambda: {"sources": {}})
    meta = ls.law_meta({"source": "nis", "frontmatter": fm})
    assert meta["date_disputes"] == [{"항목": "시행일", "원문값": "2020-11-26",
                                      "이견값": "2021-03-01", "출처": "NKLAW 제보 #29"}]
