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
# Item heading: ##### N. 머리글 — 조문 번호가 없는 법령(핵무력정책에 대하여·자위적핵보유국의 지위…·
# 당의 유일적령도체계확립의 10대 원칙 등)의 항목 머리. '##### 제N조' 가 하나도 없을 때만 조문으로
# 받는다(T7). 키는 T4 부칙 규약(라벨 접두 '부칙N')과 맞춰 '항N'.
ITEM_RE = re.compile(r"^#{5}\s+(\d+)\.\s*(.*)$", re.MULTILINE)
# 항목 머리글이 이 길이를 넘으면 제목이 아니라 본문 첫 문장으로 본다.
# 실제 제목(핵무력정책: '핵무력의 사명' 등)은 20자 미만이고, 줄바꿈으로 끊긴 문장 조각은 40자 이상이다.
ITEM_TITLE_MAX = 30

# Chapter heading: ## 제N장 제목
# Note: some files concatenate the first article text on the same line as the
# chapter heading, so we only capture up to where article text might begin.
CHAPTER_RE = re.compile(
    r"^##\s+제(\d+)장\s*(.*?)(?:제\d+조|$)",
    re.MULTILINE,
)

# 저장소 markdown 헤더 규약(수집 측과 합의): # 제N편 / ## 제N장 / ### 제N절 / ##### 제N조
# Part heading:    # 제N편 제목   (편이 없는 법령이 대부분 — 선택)
PART_RE = re.compile(r"^#\s+제(\d+)편\s*(.*)$", re.MULTILINE)
# Section heading: ### 제N절 제목 (장 안의 절 — 선택)
SECTION_RE = re.compile(r"^###\s+제(\d+)절\s*(.*)$", re.MULTILINE)
# Subsection heading: #### 제N관 제목 (절 안의 관 — 경계 계산용. DB 컬럼은 두지 않음)
SUBSECTION_RE = re.compile(r"^####\s+제(\d+)관\s*(.*)$", re.MULTILINE)
# 장이 아닌 ## 블록: 부칙(7개 법령)·서문(헌법). 부칙 아래 '##### 제1조' 는 본칙과 번호가
# 겹치므로 article 키를 '부칙N' 으로 구분한다(수집 측 규약 예외 ①).
SUPPLEMENT_RE = re.compile(r"^##\s+(부\s*칙|서\s*문)\s*(.*)$", re.MULTILINE)

# Combined pattern to split body into structural headings and articles in order
HEADING_RE = re.compile(
    r"^(?:#\s+제\d+편|#{2}\s+(?:제\d+장|부\s*칙|서\s*문)|#{3}\s+제\d+절|#{4}\s+제\d+관|#{5}\s+제\d+조)",
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


_ALIAS_CACHE: dict[str, list[str]] | None = None


def _master_aliases() -> dict[str, list[str]]:
    """북한법령_마스터목록.json 의 name_alias 를 {정식명: [별칭…]} 로 1회 로드.

    경로는 config.yaml data.master_list. 파일이 없거나 깨져도 import 는 계속된다({}).
    마스터목록은 수집 담당이 관리하므로 읽기만 한다. name_alias 는 문자열/배열 모두 허용.
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    _ALIAS_CACHE = {}
    try:
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
        )
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        path = (cfg.get("data") or {}).get("master_list")
        if not path or not os.path.exists(path):
            print(f"[warn] master_list not found ({path}) — name_alias 주입 생략")
            return _ALIAS_CACHE
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        laws = data.get("laws", []) if isinstance(data, dict) else data
        for law in laws:
            if not isinstance(law, dict):
                continue
            name = str(law.get("name") or "").strip()
            raw = law.get("name_alias")
            if not name or not raw:
                continue
            aliases = raw if isinstance(raw, list) else [raw]
            cleaned = [str(a).strip() for a in aliases if str(a).strip() and str(a).strip() != name]
            if cleaned:
                _ALIAS_CACHE[name] = cleaned
        print(f"[info] master_list aliases loaded: {len(_ALIAS_CACHE)} laws")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] master_list alias load skipped: {e}")
    return _ALIAS_CACHE


def _inject_aliases(fm: dict, law_name: str) -> dict:
    """frontmatter 에 마스터목록 name_alias 를 합쳐 넣는다(기존 값 보존, 중복 제거).

    get_by_name 의 별칭 폴백(frontmatter->'name_alias')이 이 값을 쓴다 —
    옛 오타명(예: 사회주의농총발전법)과 약칭이 같은 메커니즘으로 정식명에 닿는다.
    """
    aliases = _master_aliases()
    found = aliases.get(law_name) or aliases.get(str(fm.get("제목") or "").strip()) or []
    if not found:
        return fm
    existing = fm.get("name_alias") or []
    if isinstance(existing, str):
        existing = [existing]
    merged = list(dict.fromkeys([*[str(x) for x in existing], *found]))
    return {**fm, "name_alias": merged}


def parse_markdown_text(text: str) -> dict:
    """텍스트 자체를 받아 frontmatter/articles/full_text를 추출."""

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
    # 본문을 선형으로 훑어 현재 편(part)/장(chapter)/절(section)을 추적한다.
    articles: list[dict] = []

    part_positions: list[tuple[int, str]] = [
        (m.start(), f"제{m.group(1)}편 {m.group(2).strip()}".strip())
        for m in PART_RE.finditer(body)
    ]
    chapter_positions: list[tuple[int, str]] = [
        (m.start(), f"제{m.group(1)}장 {m.group(2).strip()}".strip())
        for m in CHAPTER_RE.finditer(body)
    ]
    section_positions: list[tuple[int, str]] = [
        (m.start(), f"제{m.group(1)}절 {m.group(2).strip()}".strip())
        for m in SECTION_RE.finditer(body)
    ]
    subsection_positions: list[tuple[int, str]] = [
        (m.start(), f"제{m.group(1)}관 {m.group(2).strip()}".strip())
        for m in SUBSECTION_RE.finditer(body)
    ]
    # 부칙/서문 블록 — 공백 제거한 라벨('부칙'/'서문')
    supplement_positions: list[tuple[int, str]] = [
        (m.start(), re.sub(r"\s+", "", m.group(1)))
        for m in SUPPLEMENT_RE.finditer(body)
    ]
    # 모든 구조 헤더 위치(조문 본문의 끝 경계 계산용)
    struct_positions = sorted(
        p for p, _ in (
            *part_positions, *chapter_positions, *section_positions,
            *subsection_positions, *supplement_positions,
        )
    )

    def _last_before(positions: list[tuple[int, str]], pos: int, after: int = -1):
        """pos 앞에 있는 마지막 헤더(단, after 보다 뒤에 있는 것만) → (위치, 이름) 또는 (None, None)."""
        found_pos, found_name = None, None
        for p, name in positions:
            if p <= pos and p > after:
                found_pos, found_name = p, name
            elif p > pos:
                break
        return found_pos, found_name

    # Collect all article matches
    article_matches = list(ARTICLE_RE.finditer(body))

    # T7: 조문('##### 제N조')이 전혀 없는 법령은 '##### N. 머리글' 항목을 조문으로 받는다.
    item_style = False
    if not article_matches:
        item_matches = list(ITEM_RE.finditer(body))
        if item_matches:
            item_style = True
            article_matches = item_matches

    for idx, m in enumerate(article_matches):
        art_num = m.group(1)
        art_title = m.group(2) or ""

        # 편 → 그 편 안의 장 → 그 장 안의 절. 이전 편/장의 장/절이 새 편/장으로
        # 넘어오지 않게, 상위 헤더 위치(after) 이후의 것만 인정한다.
        part_pos, part_name = _last_before(part_positions, m.start())
        ch_pos, chapter_name = _last_before(
            chapter_positions, m.start(), after=part_pos if part_pos is not None else -1
        )
        _, section_name = _last_before(
            section_positions, m.start(), after=ch_pos if ch_pos is not None else -1
        )

        # 부칙/서문 블록 안의 조문: 그 블록 헤더가 앞선 편/장보다 뒤에 있으면 블록 소속.
        # 편/장/절은 비우고 chapter 에 라벨을, article 키는 '부칙N' 으로 구분한다.
        art_key = art_num
        supplement_label = None
        sup_pos, sup_label = _last_before(supplement_positions, m.start())
        if sup_pos is not None and sup_pos > max(
            part_pos if part_pos is not None else -1,
            ch_pos if ch_pos is not None else -1,
        ):
            supplement_label = sup_label
            part_name, chapter_name, section_name = None, sup_label, None
            art_key = f"{sup_label}{art_num}"

        # Content: from end of this heading line to start of next article or any
        # structural heading (part/chapter/section), whichever comes first.
        content_start = m.end()
        if idx + 1 < len(article_matches):
            next_art_start = article_matches[idx + 1].start()
        else:
            next_art_start = len(body)
        next_struct_start = len(body)
        for p in struct_positions:
            if p > m.start():
                next_struct_start = p
                break

        content_end = min(next_art_start, next_struct_start)
        content = body[content_start:content_end].strip()

        kind = "조"
        if item_style:
            # 항목 머리글: 짧으면 제목, 길면(문장) 본문 첫 줄로 — 전문검색에서 빠지지 않게 한다.
            head = (m.group(2) or "").strip()
            # 완결 문장('.'·'다'로 끝남)은 길이와 무관하게 본문. 제목은 짧은 명사구만.
            # 조사로 끝나면 줄바꿈으로 끊긴 문장 조각(예: "…침략과 공격을") → 본문.
            if (len(head) > ITEM_TITLE_MAX or head.endswith((".", "다", "。"))
                    or (head and head[-1] in "을를은는이가에와과의로고며여서도")):
                art_title = ""
                content = (head + ("\n" + content if content else "")).strip()
            else:
                art_title = head
            art_key = f"항{art_num}"
            kind = "항목"
        elif supplement_label:
            kind = supplement_label

        articles.append({
            "article_number": art_key,
            "article_title": art_title.strip(),
            "content": content,
            "part": part_name,
            "chapter": chapter_name,
            "section": section_name,
            "supplement": supplement_label,  # '부칙'/'서문' 소속이면 라벨, 아니면 None
            "kind": kind,                    # '조' | '항목'(N. 머리) | '부칙' | '서문'
            "position": idx,
        })

    return {
        "frontmatter": frontmatter,
        "full_text": text,
        "articles": articles,
    }


def parse_markdown_file(file_path: str | Path) -> dict:
    """Read a markdown file from disk and parse it via parse_markdown_text."""
    return parse_markdown_text(Path(file_path).read_text(encoding="utf-8"))


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

            # T4 (e)/③: 마스터목록 name_alias(약칭·옛 오타명)를 frontmatter 에 주입 →
            # get_by_name 별칭 폴백이 정식명으로 해석한다. 날짜이견(frontmatter '날짜이견')은
            # 별도 처리 없이 jsonb 에 통째로 저장되어 law_get/law_history 가 꺼내 쓴다.
            fm = _inject_aliases(fm, law_dir.name)
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

            # --- INSERT articles ---  (part/section: T4 편·절 필드)
            for art in parsed["articles"]:
                await conn.execute(
                    """
                    INSERT INTO articles
                        (law_id, article_number, article_title, content,
                         chapter, part, section, position)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT (law_id, article_number) DO NOTHING
                    """,
                    law_id,
                    art["article_number"],
                    art["article_title"],
                    art["content"],
                    art["chapter"],
                    art.get("part"),
                    art.get("section"),
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
                # T4 (d): 개정이력 항목의 선택 키 '시행일' → effective_date (없으면 NULL)
                effective = _safe_date(entry.get("시행일"))

                await conn.execute(
                    """
                    INSERT INTO amendments (law_id, date, action, basis, effective_date)
                    VALUES ($1,$2,$3,$4,$5)
                    ON CONFLICT (law_id, date) DO NOTHING
                    """,
                    law_id,
                    amd_date,
                    action,
                    basis,
                    effective,
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
