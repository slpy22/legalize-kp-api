"""
scripts/init_db.py

Initialise the PostgreSQL database and tables for legalize-kp-api.

Usage (from project root):
    python scripts/init_db.py
"""
from __future__ import annotations

import asyncio
import sys
import os

# Allow imports from the project root (app.core.config)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from app.core.config import load_config

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_LAWS = """
CREATE TABLE IF NOT EXISTS laws (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    category            TEXT NOT NULL,
    enactment_date      DATE,
    latest_version_date DATE,
    total_articles      INT,
    chapter_count       INT,
    amendment_count     INT,
    source              TEXT,
    is_constitutional   BOOLEAN DEFAULT FALSE,
    is_ocr              BOOLEAN DEFAULT FALSE,
    ocr_confidence      TEXT,
    frontmatter         JSONB,
    full_text           TEXT,
    created_at          TIMESTAMP DEFAULT NOW()
);
"""

CREATE_ARTICLES = """
CREATE TABLE IF NOT EXISTS articles (
    id             SERIAL PRIMARY KEY,
    law_id         INT REFERENCES laws(id) ON DELETE CASCADE,
    article_number TEXT,
    article_title  TEXT,
    content        TEXT,
    chapter        TEXT,
    position       INT,
    UNIQUE(law_id, article_number)
);
"""

CREATE_AMENDMENTS = """
CREATE TABLE IF NOT EXISTS amendments (
    id      SERIAL PRIMARY KEY,
    law_id  INT REFERENCES laws(id) ON DELETE CASCADE,
    date    DATE NOT NULL,
    action  TEXT NOT NULL,
    basis   TEXT,
    UNIQUE(law_id, date)
);
"""

CREATE_LAW_VERSIONS = """
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

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_laws_fts       ON laws     USING gin(to_tsvector('simple', full_text));",
    "CREATE INDEX IF NOT EXISTS idx_articles_fts   ON articles USING gin(to_tsvector('simple', content));",
    "CREATE INDEX IF NOT EXISTS idx_laws_category  ON laws(category);",
    "CREATE INDEX IF NOT EXISTS idx_laws_name      ON laws(name);",
    "CREATE INDEX IF NOT EXISTS idx_articles_law_id ON articles(law_id);",
    "CREATE INDEX IF NOT EXISTS idx_amendments_law_id ON amendments(law_id);",
    "CREATE INDEX IF NOT EXISTS idx_law_versions_law_id ON law_versions(law_id);",
    "CREATE INDEX IF NOT EXISTS idx_law_versions_date   ON law_versions(version_date);",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ensure_database(cfg: dict) -> None:
    """Connect to 'postgres' maintenance DB and CREATE the target DB if absent."""
    db_cfg = cfg["database"]
    conn = await asyncpg.connect(
        host=db_cfg["host"],
        port=db_cfg["port"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database="postgres",          # maintenance DB always exists
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            db_cfg["database"],
        )
        if exists:
            print(f"[info] Database '{db_cfg['database']}' already exists – skipping creation.")
        else:
            # CREATE DATABASE cannot run inside a transaction; asyncpg wraps each
            # statement in one by default, so we use execute() with autocommit via
            # an explicit transaction block workaround.
            await conn.execute(f"CREATE DATABASE \"{db_cfg['database']}\"")
            print(f"[ok]   Database '{db_cfg['database']}' created.")
    finally:
        await conn.close()


async def create_schema(cfg: dict) -> None:
    """Connect to the target DB and create tables + indexes."""
    db_cfg = cfg["database"]
    conn = await asyncpg.connect(
        host=db_cfg["host"],
        port=db_cfg["port"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
    )
    try:
        async with conn.transaction():
            for ddl, label in [
                (CREATE_LAWS,        "laws"),
                (CREATE_ARTICLES,    "articles"),
                (CREATE_AMENDMENTS,  "amendments"),
                (CREATE_LAW_VERSIONS,"law_versions"),
            ]:
                await conn.execute(ddl)
                print(f"[ok]   Table '{label}' ready.")

        # Indexes run outside the transaction so that each one is autocommitted
        # (CREATE INDEX CONCURRENTLY would require it; IF NOT EXISTS is safe here).
        for idx_ddl in CREATE_INDEXES:
            await conn.execute(idx_ddl)
            # Extract index name for the log message
            idx_name = idx_ddl.split("EXISTS")[1].split()[0].strip()
            print(f"[ok]   Index '{idx_name}' ready.")

    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    # config.yaml lives in the project root; resolve relative to this script
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(config_path)

    db_cfg = cfg["database"]
    print(f"[info] Connecting to PostgreSQL at {db_cfg['host']}:{db_cfg['port']} as '{db_cfg['user']}'")

    await ensure_database(cfg)
    await create_schema(cfg)

    print("\n[done] Database initialisation complete.")


if __name__ == "__main__":
    asyncio.run(main())
