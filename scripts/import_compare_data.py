"""
scripts/import_compare_data.py

Load compare JSON files from legalize-kp/compare/ into PostgreSQL.

Tables:
  compare_mappings  — law name mappings between KP and KR
  compare_terms     — terminology pairs

Usage (from project root):
    python scripts/import_compare_data.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.core.config import load_config  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"


def _compare_dir(cfg: dict) -> Path:
    """Return the compare/ directory: config data.compare_path, else repo_path/compare."""
    data = cfg.get("data", {})
    compare_path = data.get("compare_path")
    if compare_path:
        return Path(compare_path)
    repo_path = data.get("repo_path", "E:/004_북한법/legalize-kp")
    return Path(repo_path) / "compare"


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


async def import_mappings(conn: asyncpg.Connection, compare_dir: Path) -> int:
    """
    Load law_mappings.json → DELETE + INSERT into compare_mappings.
    Supports both v1 (single kr_name) and v2 (kr_names array) schema.
    Returns the number of rows inserted.
    """
    law_mappings_path = compare_dir / "law_mappings.json"

    if not law_mappings_path.exists():
        print(f"[skip] {law_mappings_path} not found — skipping compare_mappings import.")
        return 0

    with law_mappings_path.open(encoding="utf-8") as f:
        data = json.load(f)

    mappings = data.get("mappings", [])

    async with conn.transaction():
        await conn.execute("DELETE FROM compare_mappings")
        inserted = 0
        for row in mappings:
            # v2: kr_names is already a list; v1: wrap single kr_name in a list
            kr_names = row.get("kr_names")
            if kr_names is None:
                single = row.get("kr_name") or row.get("sk_name") or ""
                kr_names = [single] if single else []

            kr_categories = row.get("kr_categories") or []
            overlap_areas = row.get("overlap_areas") or []
            kp_unique = row.get("kp_unique") or []
            kr_unique = row.get("kr_unique") or []
            article_mappings = row.get("article_mappings") or []

            await conn.execute(
                """
                INSERT INTO compare_mappings
                    (kp_name, kp_category, kr_names, kr_categories,
                     relationship, overlap_areas, kp_unique, kr_unique,
                     article_mappings, confidence, source, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
                ON CONFLICT (kp_name) DO UPDATE SET
                    kp_category = EXCLUDED.kp_category,
                    kr_names = EXCLUDED.kr_names,
                    kr_categories = EXCLUDED.kr_categories,
                    relationship = EXCLUDED.relationship,
                    overlap_areas = EXCLUDED.overlap_areas,
                    kp_unique = EXCLUDED.kp_unique,
                    kr_unique = EXCLUDED.kr_unique,
                    article_mappings = EXCLUDED.article_mappings,
                    confidence = EXCLUDED.confidence,
                    source = EXCLUDED.source,
                    notes = EXCLUDED.notes
                """,
                row.get("kp_name") or row.get("nk_name"),
                row.get("kp_category") or row.get("nk_category"),
                kr_names,                              # TEXT[] — pass as list
                kr_categories,                         # TEXT[]
                row.get("relationship") or "related",
                overlap_areas,                         # TEXT[]
                kp_unique,                             # TEXT[]
                kr_unique,                             # TEXT[]
                json.dumps(article_mappings, ensure_ascii=False),  # JSONB
                row.get("confidence") or "medium",
                row.get("source") or "ai_generated",
                row.get("notes"),
            )
            inserted += 1

    return inserted


async def import_terms(conn: asyncpg.Connection, compare_dir: Path) -> int:
    """
    Load term_pairs.json → DELETE + INSERT into compare_terms.
    Returns the number of rows inserted.
    """
    term_pairs_path = compare_dir / "term_pairs.json"

    if not term_pairs_path.exists():
        print(f"[skip] {term_pairs_path} not found — skipping compare_terms import.")
        return 0

    with term_pairs_path.open(encoding="utf-8") as f:
        data = json.load(f)

    terms = data.get("terms", [])

    async with conn.transaction():
        await conn.execute("DELETE FROM compare_terms")
        inserted = 0
        for row in terms:
            await conn.execute(
                """
                INSERT INTO compare_terms
                    (kp_term, kr_term, category, verified)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                row.get("kp"),
                row.get("kr"),
                row.get("category"),
                bool(row.get("verified", False)),
            )
            inserted += 1

    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    cfg = load_config(str(_CONFIG_PATH))
    db = cfg["database"]

    conn: asyncpg.Connection = await asyncpg.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )

    try:
        compare_dir = _compare_dir(cfg)

        mapping_count = await import_mappings(conn, compare_dir)
        term_count = await import_terms(conn, compare_dir)

        print(f"[done] compare_mappings: {mapping_count} rows inserted.")
        print(f"[done] compare_terms:    {term_count} rows inserted.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
