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


class LawRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_name(self, name: str) -> dict | None:
        # 1) 정확 매칭
        result = await self.session.execute(
            text("SELECT * FROM laws WHERE name = :name"), {"name": name}
        )
        row = result.mappings().first()
        if row:
            return dict(row)

        # 2) 별칭(옛이름) 매칭 — frontmatter JSONB 안에 former_names/옛이름 배열로 저장됨
        # 두 가지 키 모두 지원
        import json as _json
        name_jsonb = _json.dumps([name], ensure_ascii=False)
        result = await self.session.execute(
            text(
                "SELECT * FROM laws "
                "WHERE frontmatter->'옛이름' @> CAST(:n AS jsonb) "
                "   OR frontmatter->'former_names' @> CAST(:n AS jsonb) "
                "LIMIT 1"
            ),
            {"n": name_jsonb},
        )
        row = result.mappings().first()
        return dict(row) if row else None

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
                "SELECT id, law_id, version_date, action, source "
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
