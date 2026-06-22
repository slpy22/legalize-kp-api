"""
scripts/build_version_embeddings.py

law_versions 테이블의 모든 (법령 × 버전 × 조문)을 Gemini 임베딩으로 만들어
별도 Qdrant 컬렉션 `legalize_kp_law_versions` 에 적재한다.

기존 현행본 컬렉션(`legalize_kp_laws`)과 분리되어 있어, 챗봇은
- 현행 사실 질의 → legalize_kp_laws
- 시간 축 질의 ('2010년에는 ...') → legalize_kp_law_versions
와 같이 컬렉션을 골라 사용한다.

Point ID = version_id * 1_000_000 + position
  (한 버전당 조문 100만 개 이하라 충돌 없음)
Payload = {law_id, law_name, category, version_id, version_date, source,
           article_number, article_title, chapter, content_snippet}

사용:
    python scripts/build_version_embeddings.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from google import genai  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, VectorParams, PointStruct  # noqa: E402

from app.core.config import load_config  # noqa: E402


COLLECTION_NAME = "legalize_kp_law_versions"


async def fetch_versions(cfg: dict) -> list[dict]:
    db = cfg["database"]
    conn: asyncpg.Connection = await asyncpg.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )
    try:
        rows = await conn.fetch(
            """
            SELECT v.id          AS version_id,
                   v.law_id      AS law_id,
                   v.version_date,
                   v.source      AS version_source,
                   v.action,
                   v.articles,
                   l.name        AS law_name,
                   l.category    AS category
            FROM   law_versions v
            JOIN   laws         l ON v.law_id = l.id
            ORDER  BY v.law_id, v.version_date
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def flatten_articles(versions: list[dict]) -> list[dict]:
    """각 version row의 articles JSONB 를 풀어 단일 article 단위 records 로 변환."""
    records: list[dict] = []
    for v in versions:
        articles_raw = v.get("articles")
        if not articles_raw:
            continue
        if isinstance(articles_raw, str):
            try:
                articles = json.loads(articles_raw)
            except Exception:
                continue
        else:
            articles = articles_raw
        if not isinstance(articles, list):
            continue
        for idx, a in enumerate(articles):
            content = a.get("content") or ""
            if not content.strip():
                continue
            records.append({
                "point_id":      int(v["version_id"]) * 1_000_000 + int(a.get("position") or idx),
                "version_id":    int(v["version_id"]),
                "law_id":        int(v["law_id"]),
                "law_name":      v["law_name"],
                "category":      v["category"],
                "version_date":  str(v["version_date"]),
                "version_source": v.get("version_source") or "",
                "action":        v.get("action") or "",
                "article_number": str(a.get("article_number") or ""),
                "article_title":  a.get("article_title") or "",
                "chapter":        a.get("chapter") or "",
                "content":        content,
            })
    return records


def embedding_text(r: dict) -> str:
    """build_embeddings 와 동일한 방식 — law_name + article + title + content(앞 500자)."""
    return f"{r['law_name']} {r['article_number']} {r['article_title']} {r['content'][:500]}".strip()


def main() -> None:
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(cfg_path)

    emb_cfg = cfg.get("embedding", {})
    qdrant_cfg = cfg.get("qdrant", {})

    model_name = emb_cfg.get("model", "text-embedding-004")
    vector_dimension = int(emb_cfg.get("dimension", 768))
    batch_size = int(os.environ.get("EMBED_BATCH_SIZE", emb_cfg.get("batch_size", 100)))
    qdrant_host = qdrant_cfg.get("host", "localhost")
    qdrant_port = int(qdrant_cfg.get("port", 6333))

    print("[info] Fetching versions from PostgreSQL ...")
    versions = asyncio.run(fetch_versions(cfg))
    print(f"[info] {len(versions)} version rows.")

    records = flatten_articles(versions)
    total = len(records)
    print(f"[info] flattened to {total:,} article records.")

    if total == 0:
        print("[warn] no records — exiting.")
        return

    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    print(f"[info] Gemini client ready (model={model_name}).")

    qdrant = QdrantClient(host=qdrant_host, port=qdrant_port, check_compatibility=False)
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME in existing:
        print(f"[info] dropping existing collection '{COLLECTION_NAME}' for clean rebuild.")
        qdrant.delete_collection(COLLECTION_NAME)
    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=vector_dimension, distance=Distance.COSINE),
    )
    print(f"[ok] collection '{COLLECTION_NAME}' created (dim={vector_dimension}).")

    MAX_RETRIES = int(os.environ.get("EMBED_MAX_RETRIES", 3))
    RETRY_WAIT = int(os.environ.get("EMBED_RETRY_WAIT", 60))

    upserted = 0
    start = time.time()
    for s in range(0, total, batch_size):
        batch = records[s : s + batch_size]
        texts = [embedding_text(r) for r in batch]

        result = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = client.models.embed_content(model=model_name, contents=texts)
                break
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str:
                    print(f"[warn] rate-limited (attempt {attempt}/{MAX_RETRIES}), waiting {RETRY_WAIT}s ...")
                    time.sleep(RETRY_WAIT)
                else:
                    raise
        if result is None:
            print(f"[error] failed after {MAX_RETRIES} retries — stopping.")
            break

        vectors = [emb.values for emb in result.embeddings]
        points = []
        for r, v in zip(batch, vectors):
            points.append(PointStruct(
                id=r["point_id"],
                vector=v,
                payload={
                    "law_id":          r["law_id"],
                    "law_name":        r["law_name"],
                    "category":        r["category"],
                    "version_id":      r["version_id"],
                    "version_date":    r["version_date"],
                    "version_source":  r["version_source"],
                    "action":          r["action"],
                    "article_number":  r["article_number"],
                    "article_title":   r["article_title"],
                    "chapter":         r["chapter"],
                    "content_snippet": r["content"][:200],
                },
            ))
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
        upserted += len(points)

        elapsed = time.time() - start
        rate = upserted / elapsed if elapsed > 0 else 0
        eta = (total - upserted) / rate if rate > 0 else 0
        print(f"[progress] {upserted:>6}/{total}  elapsed={elapsed:.0f}s  rate={rate:.1f}/s  ETA={eta:.0f}s")
        if s + batch_size < total:
            time.sleep(float(os.environ.get("EMBED_BATCH_SLEEP", 1)))

    # version_date 필터를 빠르게 하기 위한 인덱스 — 키워드 인덱스로 보존
    try:
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="version_date",
            field_schema="keyword",
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="law_id",
            field_schema="integer",
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="law_name",
            field_schema="keyword",
        )
        print("[ok] payload indexes created (version_date, law_id, law_name).")
    except Exception as e:
        print(f"[warn] payload index creation failed: {e}")

    print()
    print(f"[done] Finished in {time.time()-start:.1f}s - collection '{COLLECTION_NAME}' has {upserted:,} points.")


if __name__ == "__main__":
    main()
