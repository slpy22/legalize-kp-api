from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _to_date(value) -> date | None:
    """문자열/date 모두 date 객체로 변환. 변환 불가 시 None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _legacy_map() -> dict[str, str]:
    """config legacy_names — 데이터에서 제거된 옛 표기(오타명 등) → 정식명."""
    try:
        from app.core.config import get_config
        m = (get_config() or {}).get("legacy_names") or {}
        return {str(k).strip(): str(v).strip() for k, v in m.items()}
    except Exception:  # noqa: BLE001
        return {}


def canonical_name(name: str) -> str:
    """옛 표기를 정식명으로(해당 없으면 그대로)."""
    s = (name or "").strip()
    return _legacy_map().get(s, s)


class LawRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_name(self, name: str) -> dict | None:
        # 0) 옛 표기(config legacy_names) → 정식명
        name = canonical_name(name)
        # 1) 정확 매칭
        result = await self.session.execute(
            text("SELECT * FROM laws WHERE name = :name"), {"name": name}
        )
        row = result.mappings().first()
        if row:
            return dict(row)

        # 2) 별칭 매칭 — frontmatter JSONB 의 옛이름/former_names/name_alias 배열.
        #    name_alias 는 import 시 마스터목록(북한법령_마스터목록.json)에서 주입된다.
        import json as _json
        name_jsonb = _json.dumps([name], ensure_ascii=False)
        result = await self.session.execute(
            text(
                "SELECT * FROM laws "
                "WHERE frontmatter->'옛이름' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'former_names' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'name_alias' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'abbreviations' @> CAST(:n AS jsonb) "
                "LIMIT 1"
            ),
            {"n": name_jsonb},
        )
        row = result.mappings().first()
        if row:
            return dict(row)

        # 3) 마지막 어절(short) 폴백 — '헌법' → '조선민주주의인민공화국 헌법'. 후보가 정확히
        #    하나일 때만. T8 #74 로 resolve_name 은 이 단계를 자동 해석하지 않지만, get 은 기존
        #    URL(/law/헌법)·MCP 호출 호환을 위해 유지하고 서비스가 meta.name_warning 으로 알린다.
        #    마스터목록 abbreviations(T9)가 들어오면 2) 에서 해석되어 이 단계에 오지 않는다.
        short = name.strip()
        if short and " " not in short:
            result = await self.session.execute(
                text("SELECT * FROM laws WHERE name LIKE :pat LIMIT 2"),
                {"pat": f"% {short}"},
            )
            rows = result.mappings().all()
            if len(rows) == 1:
                return dict(rows[0])
        return None

    async def resolve_name(self, name: str) -> dict:
        """이름/별칭/약칭을 정식 법령명으로 해석한 결과를 설명과 함께 반환.

        {"input", "resolved": 정식명|None, "law_id", "matched_by": exact|legacy|alias|None,
         "candidates": [{id, name, matched_by: "short"}…], "hint": str|None}
        alias 단계는 옛이름/former_names/name_alias/abbreviations(마스터목록 약칭) 를 본다.
        마지막 어절 일치(short)는 T8 #74 부터 resolved 로 올리지 않고 candidates 로만 돌려준다.
        """
        import json as _json
        raw = name
        name = canonical_name(name)
        legacy = name != (raw or "").strip()
        exact = await self.session.execute(
            text("SELECT id, name FROM laws WHERE name = :name"), {"name": name}
        )
        r = exact.mappings().first()
        if r:
            return {"input": raw, "resolved": r["name"], "law_id": r["id"],
                    "matched_by": "legacy" if legacy else "exact", "candidates": [], "hint": None}
        name_jsonb = _json.dumps([name], ensure_ascii=False)
        alias = await self.session.execute(
            text(
                "SELECT id, name FROM laws "
                "WHERE frontmatter->'옛이름' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'former_names' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'name_alias' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'abbreviations' @> CAST(:n AS jsonb) LIMIT 1"
            ),
            {"n": name_jsonb},
        )
        r = alias.mappings().first()
        if r:
            return {"input": name, "resolved": r["name"], "law_id": r["id"],
                    "matched_by": "alias", "candidates": [], "hint": None}
        # T8 #74: 마지막 어절(short) 일치는 자동 해석하지 않는다 — '사회보장법' 이 별개 법령명일
        # 수 있는데 '사회보험 및 사회보장법' 에 조용히 붙었다(논문 팀 4차 #48·7차 #74).
        # 후보로만 돌려주고 hint 로 알린다. 의도된 약칭(헌법 등)은 마스터목록 abbreviations →
        # frontmatter->'abbreviations' 로 들어와 위 alias 단계에서 해석된다.
        short = name.strip()
        cands: list[dict] = []
        hint = None
        if short and " " not in short:
            res = await self.session.execute(
                text("SELECT id, name FROM laws WHERE name LIKE :pat ORDER BY name LIMIT 20"),
                {"pat": f"% {short}"},
            )
            cands = [{**dict(x), "matched_by": "short"} for x in res.mappings().all()]
            if cands:
                hint = ("마지막 어절이 같은 법령 후보입니다. 등록된 약칭(abbreviations)이 아니므로 "
                        "자동 해석하지 않았습니다 — 별개 법령명일 수 있습니다.")
        return {"input": name, "resolved": None, "law_id": None,
                "matched_by": None, "candidates": cands, "hint": hint}

    async def get_by_id(self, law_id: int) -> dict | None:
        result = await self.session.execute(
            text("SELECT * FROM laws WHERE id = :id"), {"id": law_id}
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_articles(
        self, law_id: int, article_number: str | None = None, grep: str | None = None
    ) -> list[dict]:
        query = "SELECT * FROM articles WHERE law_id = :law_id"
        params: dict = {"law_id": law_id}

        if article_number is not None:
            query += " AND article_number = :article_number"
            params["article_number"] = article_number

        if grep is not None:
            query += " AND content ILIKE :grep"
            params["grep"] = f"%{grep}%"

        query += " ORDER BY position"
        result = await self.session.execute(text(query), params)
        return [dict(r) for r in result.mappings().all()]

    async def get_amendments(self, law_id: int) -> list[dict]:
        result = await self.session.execute(
            text("SELECT * FROM amendments WHERE law_id = :law_id ORDER BY date"),
            {"law_id": law_id},
        )
        return [dict(r) for r in result.mappings().all()]

    async def list_by_category(self, category: str) -> list[dict]:
        result = await self.session.execute(
            text("SELECT * FROM laws WHERE category = :category ORDER BY name"),
            {"category": category},
        )
        return [dict(r) for r in result.mappings().all()]

    # ── law_versions (버전별 본문) ─────────────────────────────────────────

    async def list_versions(self, law_id: int) -> list[dict]:
        """주어진 법령에 대해 적재된 모든 버전을 일자 오름차순으로 반환.

        각 row에는 articles/full_text를 제외한 메타만 포함 — 페이로드 절감용.
        """
        result = await self.session.execute(
            text(
                "SELECT id, law_id, version_date, action, source, "
                "       frontmatter->>'OCR여부' AS ocr_flag, "
                "       frontmatter->>'정본여부' AS auth_flag, "
                "       frontmatter->>'발췌본' AS excerpt_flag "
                "FROM law_versions WHERE law_id = :law_id "
                "ORDER BY version_date"
            ),
            {"law_id": law_id},
        )
        rows = [dict(r) for r in result.mappings().all()]
        for r in rows:
            d = r.get("version_date")
            if d is not None:
                r["version_date"] = str(d)
        return rows

    async def get_version(self, law_id: int, version_date: str) -> dict | None:
        """(law_id, version_date) 조합으로 단일 버전 본문 반환."""
        d = _to_date(version_date)
        if d is None:
            return None
        result = await self.session.execute(
            text(
                "SELECT id, law_id, version_date, action, source, "
                "       full_text, articles, frontmatter "
                "FROM law_versions "
                "WHERE law_id = :law_id AND version_date = :d "
                "LIMIT 1"
            ),
            {"law_id": law_id, "d": d},
        )
        row = result.mappings().first()
        if not row:
            return None
        d = dict(row)
        if d.get("version_date") is not None:
            d["version_date"] = str(d["version_date"])
        # articles/frontmatter 는 JSONB → 그대로 dict/list 로 들어옴
        return d

    async def list_categories(self) -> list[dict]:
        result = await self.session.execute(
            text(
                "SELECT category, COUNT(*) AS count FROM laws "
                "GROUP BY category ORDER BY category"
            )
        )
        return [dict(r) for r in result.mappings().all()]

    async def count_laws(self) -> int:
        result = await self.session.execute(text("SELECT COUNT(*) AS cnt FROM laws"))
        row = result.mappings().first()
        return row["cnt"] if row else 0

    async def list_laws(
        self, category: str | None = None, offset: int = 0, limit: int = 100
    ) -> tuple[list[dict], int]:
        """전체 법령 목록(메타만, 본문 제외) — 페이지네이션. (rows, total) 반환.

        논문 팀 R4: 이름을 모르는 법령도 찾을 수 있도록 전체 목록을 돌려준다.
        """
        where = "WHERE category = :category" if category else ""
        params: dict = {"category": category} if category else {}
        cnt = await self.session.execute(
            text(f"SELECT COUNT(*) AS cnt FROM laws {where}"), params
        )
        total = (cnt.mappings().first() or {}).get("cnt", 0)
        result = await self.session.execute(
            text(
                "SELECT id, name, category, enactment_date, latest_version_date, "
                "       total_articles, chapter_count, amendment_count, source, "
                "       frontmatter->'name_alias' AS name_alias "
                f"FROM laws {where} ORDER BY name LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
        )
        rows = []
        for r in result.mappings().all():
            d = dict(r)
            for k in ("enactment_date", "latest_version_date"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            rows.append(d)
        return rows, int(total)

    async def provenance_for_names(self, names: list[str]) -> dict[str, dict]:
        """법령명 목록 → {name: {source, is_ocr, auth_flag}} (검색 결과에 출처·OCR·정본 표시용, T7)."""
        clean = [n for n in dict.fromkeys(names) if n]
        if not clean:
            return {}
        result = await self.session.execute(
            text(
                "SELECT name, source, is_ocr, frontmatter->>'정본여부' AS auth_flag, "
                "       frontmatter->>'발췌본' AS excerpt_flag "
                "FROM laws WHERE name = ANY(CAST(:names AS text[]))"
            ),
            {"names": clean},
        )
        return {r["name"]: dict(r) for r in result.mappings().all()}
