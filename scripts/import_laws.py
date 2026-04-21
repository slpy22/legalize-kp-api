"""
scripts/import_laws.py

Parse all North Korean law Markdown files from the kp/ directory
and import them into PostgreSQL (laws, articles, amendments).

Usage (from project root):
    python scripts/import_laws.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
import yaml  # noqa: E402

from app.core.config import load_config  # noqa: E402

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Article heading: ##### 제N조 (제목)  or  ##### 제N조
ARTICLE_RE = re.compile(
    r"^#{5}\s+제(\d+)조\s*(?:[\(（](.+?)[\)）])?",
    re.MULTILINE,
)

# Chapter heading: ## 제N장 제목
# Note: some files concatenate the first article text on the same line as the
# chapter heading, so we only capture up to where article text might begin.
CHAPTER_RE = re.compile(
    r"^##\s+제(\d+)장\s*(.*?)(?:제\d+조|$)",
    re.MULTILINE,
)

# Combined pattern to split body into chapters and articles in order
HEADING_RE = re.compile(
    r"^(?:#{2}\s+제\d+장|#{5}\s+제\d+조)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _safe_date(value: Any) -> date | None:
    """Convert a frontmatter date value to a Python date, or None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip().strip("'\"")
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _jsonb_default(obj: Any) -> Any:
    """JSON serialiser fallback for date objects inside frontmatter."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def parse_markdown_file(file_path: str | Path) -> dict:
    """
    Parse a single law Markdown file.

    Returns::

        {
            "frontmatter": { ... },
            "full_text": "...",
            "articles": [
                {
                    "article_number": "1",
                    "article_title": "...",
                    "content": "...",
                    "chapter": "제1장 ...",
                    "position": 0,
                },
                ...
            ],
        }
    """
    text = Path(file_path).read_text(encoding="utf-8")

    # --- frontmatter ---
    frontmatter: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                frontmatter = {}
            body = parts[2]

    # --- build ordered list of heading positions ---
    # We need to walk through the body linearly to track the current chapter.
    current_chapter: str | None = None
    articles: list[dict] = []

    # Collect all chapter heading positions
    chapter_positions: list[tuple[int, str]] = []
    for m in CHAPTER_RE.finditer(body):
        num = m.group(1)
        title = m.group(2).strip()
        chapter_positions.append((m.start(), f"제{num}장 {title}".strip()))

    # Collect all article matches
    article_matches = list(ARTICLE_RE.finditer(body))

    for idx, m in enumerate(article_matches):
        art_num = m.group(1)
        art_title = m.group(2) or ""

        # Determine which chapter this article belongs to
        current_chapter_name = None
        for ch_pos, ch_name in chapter_positions:
            if ch_pos <= m.start():
                current_chapter_name = ch_name
            else:
                break

        # Content: from end of this heading line to start of next article/chapter
        content_start = m.end()
        # Find the next article or chapter heading
        if idx + 1 < len(article_matches):
            next_art_start = article_matches[idx + 1].start()
        else:
            next_art_start = len(body)

        # Also check if a chapter heading appears before the next article
        next_chapter_start = len(body)
        for ch_pos, _ in chapter_positions:
            if ch_pos > m.start():
                next_chapter_start = ch_pos
                break

        content_end = min(next_art_start, next_chapter_start)
        content = body[content_start:content_end].strip()

        articles.append({
            "article_number": art_num,
            "article_title": art_title.strip(),
            "content": content,
            "chapter": current_chapter_name,
            "position": idx,
        })

    return {
        "frontmatter": frontmatter,
        "full_text": text,
        "articles": articles,
    }


# ---------------------------------------------------------------------------
# Database import
# ---------------------------------------------------------------------------


async def import_all(kp_dir: str) -> None:
    """Walk *kp_dir*, parse every law, and INSERT into PostgreSQL."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(config_path)
    db = cfg["database"]

    conn: asyncpg.Connection = await asyncpg.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )

    try:
        # Clear existing data
        async with conn.transaction():
            await conn.execute("DELETE FROM amendments")
            await conn.execute("DELETE FROM articles")
            await conn.execute("DELETE FROM laws")
        print("[info] Cleared existing data.")

        kp = Path(kp_dir)
        dirs = sorted([d for d in kp.iterdir() if d.is_dir()])
        total = len(dirs)
        law_count = 0
        article_count = 0
        amendment_count = 0

        for i, law_dir in enumerate(dirs, 1):
            # Find the markdown file
            md_file = law_dir / "헌법.md"
            is_constitutional = True
            if not md_file.exists():
                md_file = law_dir / "법령.md"
                is_constitutional = False
            if not md_file.exists():
                continue

            parsed = parse_markdown_file(md_file)
            fm = parsed["frontmatter"]

            # --- INSERT law ---
            law_name = fm.get("제목") or law_dir.name
            category = fm.get("카테고리") or "미분류"
            enactment_date = _safe_date(fm.get("채택일"))
            latest_version_date = _safe_date(fm.get("최신버전일"))
            total_articles = fm.get("조문수")
            if total_articles is not None:
                try:
                    total_articles = int(total_articles)
                except (ValueError, TypeError):
                    total_articles = None
            chapter_count = fm.get("장수")
            if chapter_count is not None:
                try:
                    chapter_count = int(chapter_count)
                except (ValueError, TypeError):
                    chapter_count = None
            amend_count = fm.get("개정횟수")
            if amend_count is not None:
                try:
                    amend_count = int(amend_count)
                except (ValueError, TypeError):
                    amend_count = None
            source = fm.get("출처")
            is_ocr = bool(fm.get("OCR여부", False))
            ocr_confidence = fm.get("OCR신뢰도")
            if ocr_confidence is not None:
                ocr_confidence = str(ocr_confidence)

            frontmatter_json = json.dumps(fm, ensure_ascii=False, default=_jsonb_default)

            law_id = await conn.fetchval(
                """
                INSERT INTO laws
                    (name, category, enactment_date, latest_version_date,
                     total_articles, chapter_count, amendment_count,
                     source, is_constitutional, is_ocr, ocr_confidence,
                     frontmatter, full_text)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13)
                RETURNING id
                """,
                law_name,
                category,
                enactment_date,
                latest_version_date,
                total_articles,
                chapter_count,
                amend_count,
                source,
                is_constitutional,
                is_ocr,
                ocr_confidence,
                frontmatter_json,
                parsed["full_text"],
            )
            law_count += 1

            # --- INSERT articles ---
            for art in parsed["articles"]:
                await conn.execute(
                    """
                    INSERT INTO articles
                        (law_id, article_number, article_title, content, chapter, position)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (law_id, article_number) DO NOTHING
                    """,
                    law_id,
                    art["article_number"],
                    art["article_title"],
                    art["content"],
                    art["chapter"],
                    art["position"],
                )
                article_count += 1

            # --- INSERT amendments ---
            amendments = fm.get("개정이력") or []
            seen_dates: set[str] = set()
            for entry in amendments:
                if not isinstance(entry, dict):
                    continue
                amd_date = _safe_date(entry.get("일자"))
                if amd_date is None:
                    continue
                # Handle duplicate dates within the same law
                date_key = amd_date.isoformat()
                if date_key in seen_dates:
                    continue
                seen_dates.add(date_key)

                action = str(entry.get("내용", ""))
                basis = entry.get("시행근거") or None

                await conn.execute(
                    """
                    INSERT INTO amendments (law_id, date, action, basis)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (law_id, date) DO NOTHING
                    """,
                    law_id,
                    amd_date,
                    action,
                    basis,
                )
                amendment_count += 1

            if i % 50 == 0 or i == total:
                print(f"[progress] {i}/{total} directories processed "
                      f"({law_count} laws, {article_count} articles, {amendment_count} amendments)")

        print(f"\n[done] Import complete: "
              f"{law_count} laws, {article_count} articles, {amendment_count} amendments")

    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(config_path)
    kp_dir = cfg.get("data", {}).get("kp_dir", "E:/004_북한법/legalize-kp/kp")
    asyncio.run(import_all(kp_dir))
