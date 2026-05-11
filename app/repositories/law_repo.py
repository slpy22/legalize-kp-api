from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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
