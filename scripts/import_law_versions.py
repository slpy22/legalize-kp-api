"""
scripts/import_law_versions.py

pipeline이 dump한 versions.json을 읽어 PostgreSQL law_versions 테이블에 적재.

Input: pipeline의 `python main.py --emit-versions <path>` 결과 JSON
       (스키마: [{"law_name", "version_date", "action", "file_path", "markdown"}, ...])

Usage (컨테이너 안에서):
    python scripts/import_law_versions.py /data/legalize-kp/versions.json

테이블이 없으면 자동 생성한다.
같은 (law_id, version_date) 키는 ON CONFLICT UPDATE로 멱등 처리.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.core.config import load_config  # noqa: E402
from scripts.import_laws import parse_markdown_text, _jsonb_default, _safe_date  # noqa: E402


DDL_LAW_VERSIONS = """
CREATE TABLE IF NOT EXISTS law_versions (
    id            SERIAL PRIMARY KEY,
    law_id        INT NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
    version_date  DATE NOT NULL,
    action        TEXT,
    source        TEXT,
    full_text     TEXT,
    articles      JSONB,
    frontmatter   JSONB,
    UNIQUE(law_id, version_date)
);
"""

DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_law_versions_law_id ON law_versions(law_id);",
    "CREATE INDEX IF NOT EXISTS idx_law_versions_date   ON law_versions(version_date);",
]


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


async def main(input_path: str) -> None:
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(cfg_path)
    db = cfg["database"]

    with open(input_path, encoding="utf-8") as f:
        entries: list[dict] = json.load(f)
    print(f"[info] Loaded {len(entries)} version entries from {input_path}")

    conn: asyncpg.Connection = await asyncpg.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )

    try:
        # 1) 스키마 보장
        async with conn.transaction():
            await conn.execute(DDL_LAW_VERSIONS)
        for ddl in DDL_INDEXES:
            await conn.execute(ddl)
        print("[ok] law_versions schema ready.")

        # 2) 법령명 → law_id 매핑 (옛이름 별칭 포함)
        law_rows = await conn.fetch(
            "SELECT id, name, frontmatter FROM laws"
        )
        name_to_id: dict[str, int] = {}
        for r in law_rows:
            name_to_id[r["name"]] = r["id"]
            fm_raw = r["frontmatter"]
            if isinstance(fm_raw, str):
                try:
                    fm = json.loads(fm_raw)
                except Exception:
                    fm = {}
            else:
                fm = fm_raw or {}
            for alias in (fm.get("옛이름") or fm.get("former_names") or []):
                name_to_id.setdefault(alias, r["id"])
        print(f"[info] {len(law_rows)} laws indexed ({len(name_to_id)} name keys incl. aliases).")

        # 3) 기존 데이터 클리어 — 멱등 import (전 사이클 dump 와 다른 버전이 남지 않도록)
        await conn.execute("DELETE FROM law_versions")
        print("[info] cleared existing law_versions rows.")

        # 4) 각 entry parse + INSERT
        skipped = 0
        inserted = 0
        for e in entries:
            law_name = e["law_name"]
            version_date = _parse_date(e["version_date"])
            if version_date is None:
                skipped += 1
                continue
            law_id = name_to_id.get(law_name)
            if law_id is None:
                # alias 매칭도 실패 — 미입수/placeholder 법령
                skipped += 1
                continue

            md = e.get("markdown") or ""
            parsed = parse_markdown_text(md)
            fm = parsed["frontmatter"] or {}
            source = fm.get("출처")
            text_unavailable = bool(fm.get("텍스트미확보"))

            # placeholder(텍스트 미확보) 버전은 본문 없이 메타만 기록
            full_text = parsed["full_text"]
            articles_list = parsed["articles"]
            if text_unavailable:
                full_text = ""
                articles_list = []

            await conn.execute(
                """
                INSERT INTO law_versions
                    (law_id, version_date, action, source, full_text, articles, frontmatter)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                ON CONFLICT (law_id, version_date) DO UPDATE
                  SET action      = EXCLUDED.action,
                      source      = EXCLUDED.source,
                      full_text   = EXCLUDED.full_text,
                      articles    = EXCLUDED.articles,
                      frontmatter = EXCLUDED.frontmatter
                """,
                law_id,
                version_date,
                e.get("action"),
                source,
                full_text,
                json.dumps(articles_list, ensure_ascii=False, default=_jsonb_default),
                json.dumps(fm, ensure_ascii=False, default=_jsonb_default),
            )
            inserted += 1

            if inserted % 100 == 0:
                print(f"[progress] {inserted} versions inserted ({skipped} skipped)")

        print()
        print(f"[done] Import complete: {inserted} versions, {skipped} skipped.")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", help="versions.json 경로 (pipeline --emit-versions 결과)")
    args = p.parse_args()
    asyncio.run(main(args.input))
