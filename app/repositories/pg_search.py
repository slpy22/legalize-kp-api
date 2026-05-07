from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PgSearchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search_laws(
        self, query: str, category: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """법령 검색: FTS 전문 검색 + ILIKE 부분 매칭 + 법령명 매칭 통합.

        한국어는 공백 기반 FTS만으로 부분 매칭이 안 되므로,
        FTS 결과와 ILIKE 결과를 UNION하여 중복 제거합니다.

        Returns (rows, total_count) — rows are sliced by offset/limit after dedup.
        """
        # 단어별 ILIKE 조건 생성 (하나라도 포함되면 매칭 — OR)
        # 단, 매칭된 단어 수에 따라 rank를 높여서 AND에 가까운 결과가 상위 노출
        words = query.split()
        like_conditions_name = " OR ".join(f"name ILIKE :w{i}" for i in range(len(words)))
        like_conditions_text = " OR ".join(f"full_text ILIKE :w{i}" for i in range(len(words)))

        # 매칭된 단어 수 계산 (이름, 본문 각각)
        name_match_count = " + ".join(
            f"CASE WHEN name ILIKE :w{i} THEN 1 ELSE 0 END" for i in range(len(words))
        )
        text_match_count = " + ".join(
            f"CASE WHEN full_text ILIKE :w{i} THEN 1 ELSE 0 END" for i in range(len(words))
        )
        word_count = max(len(words), 1)

        params: dict = {"query": query}
        for i, w in enumerate(words):
            params[f"w{i}"] = f"%{w}%"

        cat_filter = ""
        if category is not None:
            cat_filter = "AND category = :category"
            params["category"] = category

        sql = f"""
            SELECT * FROM (
                -- 1) FTS 전문 검색
                SELECT *, ts_rank(to_tsvector('simple', full_text),
                    plainto_tsquery('simple', :query)) AS rank
                FROM laws
                WHERE to_tsvector('simple', full_text) @@ plainto_tsquery('simple', :query)
                {cat_filter}

                UNION

                -- 2) 법령명 단어별 부분 매칭 (매칭 단어 수 비례 rank)
                SELECT *, (0.5 * ({name_match_count})::float / {word_count}) AS rank
                FROM laws
                WHERE {like_conditions_name or 'FALSE'}
                {cat_filter}

                UNION

                -- 3) 본문 단어별 부분 매칭 (매칭 단어 수 비례 rank)
                SELECT *, (0.1 * ({text_match_count})::float / {word_count}) AS rank
                FROM laws
                WHERE {like_conditions_text or 'FALSE'}
                {cat_filter}
            ) combined
            ORDER BY rank DESC
        """
        result = await self.session.execute(text(sql), params)
        # UNION으로 중복 가능 — id 기준 중복 제거 (가장 높은 rank 유지)
        seen = {}
        for row in result.mappings().all():
            row_dict = dict(row)
            law_id = row_dict["id"]
            if law_id not in seen or row_dict["rank"] > seen[law_id]["rank"]:
                seen[law_id] = row_dict
        rows = sorted(seen.values(), key=lambda r: r["rank"], reverse=True)
        total = len(rows)
        return rows[offset:offset + limit], total

    async def search_articles(
        self, query: str, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """조문 검색: FTS + ILIKE 부분 매칭 통합.

        Returns (rows, total_count).
        """
        words = query.split()
        word_conditions = " OR ".join(
            f"(a.content ILIKE :aw{i} OR a.article_title ILIKE :aw{i})" for i in range(len(words))
        )
        art_params: dict = {"query": query}
        for i, w in enumerate(words):
            art_params[f"aw{i}"] = f"%{w}%"

        sql = f"""
            SELECT * FROM (
                -- 1) FTS
                SELECT a.*, l.name AS law_name, l.category,
                    ts_rank(to_tsvector('simple', a.content),
                    plainto_tsquery('simple', :query)) AS rank
                FROM articles a JOIN laws l ON a.law_id = l.id
                WHERE to_tsvector('simple', a.content) @@ plainto_tsquery('simple', :query)

                UNION

                -- 2) 단어별 ILIKE 부분 매칭
                SELECT a.*, l.name AS law_name, l.category, 0.1 AS rank
                FROM articles a JOIN laws l ON a.law_id = l.id
                WHERE {word_conditions or 'FALSE'}
            ) combined
            ORDER BY rank DESC
        """
        result = await self.session.execute(text(sql), art_params)
        seen = {}
        for row in result.mappings().all():
            row_dict = dict(row)
            art_id = row_dict["id"]
            if art_id not in seen or row_dict["rank"] > seen[art_id]["rank"]:
                seen[art_id] = row_dict
        rows = sorted(seen.values(), key=lambda r: r["rank"], reverse=True)
        total = len(rows)
        return rows[offset:offset + limit], total
