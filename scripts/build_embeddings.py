"""
scripts/build_embeddings.py

Fetch all articles from PostgreSQL, create Google Gemini embeddings, and
store the resulting vectors in Qdrant.

Usage (from project root):
    python scripts/build_embeddings.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from google import genai  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, VectorParams, PointStruct  # noqa: E402

from app.core.config import load_config  # noqa: E402


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def fetch_articles(cfg: dict) -> list[dict]:
    """Connect to PostgreSQL and return every article joined with its law."""
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
            SELECT a.id,
                   a.article_number,
                   a.article_title,
                   a.content,
                   a.chapter,
                   l.name     AS law_name,
                   l.category
            FROM   articles a
            JOIN   laws     l ON a.law_id = l.id
            ORDER  BY a.id
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def create_embedding_text(article: dict) -> str:
    """Build the text string that will be embedded for one article."""
    law_name      = article.get("law_name") or ""
    article_number = article.get("article_number") or ""
    article_title  = article.get("article_title") or ""
    content        = (article.get("content") or "")[:500]
    return f"{law_name} {article_number} {article_title} {content}".strip()


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> None:
    # ------------------------------------------------------------------
    # 1. Load config
    # ------------------------------------------------------------------
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    cfg = load_config(config_path)

    emb_cfg     = cfg.get("embedding", {})
    qdrant_cfg  = cfg.get("qdrant", {})

    model_name       = emb_cfg.get("model",      "text-embedding-004")
    vector_dimension = int(emb_cfg.get("dimension", 768))
    # 환경변수로 batch_size override 가능 — 키 quota 가 낮을 때 보수적으로 (예: 5)
    batch_size       = int(os.environ.get("EMBED_BATCH_SIZE", emb_cfg.get("batch_size", 100)))
    collection_name  = qdrant_cfg.get("collection", "legalize_kp_laws")
    qdrant_host      = qdrant_cfg.get("host", "localhost")
    qdrant_port      = int(qdrant_cfg.get("port", 6333))

    # ------------------------------------------------------------------
    # 2. Fetch articles from PostgreSQL
    # ------------------------------------------------------------------
    print("[info] Connecting to PostgreSQL and fetching articles ...")
    articles = asyncio.run(fetch_articles(cfg))
    total = len(articles)
    print(f"[info] Fetched {total:,} articles.")

    if total == 0:
        print("[warn] No articles found. Exiting.")
        return

    # ------------------------------------------------------------------
    # 3. Initialise Google Gemini client
    # ------------------------------------------------------------------
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        client = genai.Client(api_key=api_key)
    else:
        # Fall back to Application Default Credentials / Vertex AI auth
        client = genai.Client()
    print(f"[info] Gemini client ready (model={model_name}).")

    # ------------------------------------------------------------------
    # 4. Initialise Qdrant (incremental — reuse collection if it exists)
    # ------------------------------------------------------------------
    qdrant = QdrantClient(host=qdrant_host, port=qdrant_port)

    # --reset: 기존 컬렉션을 통째로 비우고 재구축(전체 재임베딩). 미지정 시 증분 모드.
    # 주의: 증분 모드는 article id 로 skip 하므로, Postgres 재적재로 id가 재배정된
    # 경우 옛 벡터가 orphan 으로 남는다. 전체 재구축 시에는 반드시 --reset 사용.
    reset = "--reset" in sys.argv
    existing = [c.name for c in qdrant.get_collections().collections]
    if reset and collection_name in existing:
        print(f"[info] --reset: dropping existing collection '{collection_name}' "
              f"for a clean full rebuild.")
        qdrant.delete_collection(collection_name)
        existing = [c.name for c in qdrant.get_collections().collections]
    if collection_name not in existing:
        print(f"[info] Creating Qdrant collection '{collection_name}' "
              f"(dim={vector_dimension}, distance=COSINE) ...")
        qdrant.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_dimension, distance=Distance.COSINE),
        )
    else:
        print(f"[info] Qdrant collection '{collection_name}' already exists - incremental mode.")

    # Build set of IDs already in Qdrant so we can skip them
    existing_ids: set[int] = set()
    try:
        scroll_result = qdrant.scroll(
            collection_name=collection_name,
            limit=100_000,
            with_payload=False,
            with_vectors=False,
        )
        existing_ids = {p.id for p in scroll_result[0]}
        if existing_ids:
            print(f"[info] Found {len(existing_ids):,} existing points - will skip them.")
    except Exception:
        pass  # collection may be empty

    # ------------------------------------------------------------------
    # 5. Filter out already-embedded articles (incremental)
    # ------------------------------------------------------------------
    new_articles = [a for a in articles if a["id"] not in existing_ids]
    new_total = len(new_articles)
    if new_total == 0:
        print("[info] All articles already embedded. Nothing to do.")
    else:
        print(f"[info] {new_total:,} new articles to embed (skipping {total - new_total:,} existing).")

    # ------------------------------------------------------------------
    # 6. Batch embed and upsert with rate-limit handling
    # ------------------------------------------------------------------
    # 키 quota 가 낮으면 환경변수로 더 보수적 (재시도↑, 대기↑)
    MAX_RETRIES = int(os.environ.get("EMBED_MAX_RETRIES", 3))
    RETRY_WAIT  = int(os.environ.get("EMBED_RETRY_WAIT", 60))

    print(f"[info] Starting embedding in batches of {batch_size} ...")
    upserted = 0
    start_time = time.time()

    for batch_start in range(0, new_total, batch_size):
        batch = new_articles[batch_start : batch_start + batch_size]

        # Build text strings for this batch
        texts = [create_embedding_text(art) for art in batch]

        # Call Gemini embedding API with retry on rate limit
        result = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = client.models.embed_content(model=model_name, contents=texts)
                break
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str:
                    print(f"[warn] Rate limited (attempt {attempt}/{MAX_RETRIES}). "
                          f"Waiting {RETRY_WAIT}s ...")
                    time.sleep(RETRY_WAIT)
                else:
                    raise

        if result is None:
            print(f"[error] Failed after {MAX_RETRIES} retries. Stopping.")
            break

        # result.embeddings is a list of ContentEmbedding objects; each has .values
        vectors = [emb.values for emb in result.embeddings]

        # Build Qdrant points
        points = []
        for article, vector in zip(batch, vectors):
            content_snippet = (article.get("content") or "")[:200]
            points.append(
                PointStruct(
                    id=article["id"],
                    vector=vector,
                    payload={
                        "law_name":       article.get("law_name") or "",
                        "article_number": article.get("article_number") or "",
                        "article_title":  article.get("article_title") or "",
                        "content_snippet": content_snippet,
                        "category":       article.get("category") or "",
                        "chapter":        article.get("chapter") or "",
                    },
                )
            )

        qdrant.upsert(collection_name=collection_name, points=points)
        upserted += len(points)

        batch_end = min(batch_start + batch_size, new_total)
        elapsed   = time.time() - start_time
        rate      = upserted / elapsed if elapsed > 0 else 0
        eta_sec   = (new_total - upserted) / rate if rate > 0 else 0
        print(
            f"[progress] {batch_end:>6}/{new_total}  "
            f"upserted={upserted:,}  "
            f"elapsed={elapsed:.0f}s  "
            f"rate={rate:.1f}/s  "
            f"ETA={eta_sec:.0f}s"
        )

        # Rate-limit courtesy: sleep between batches (환경변수로 조정 가능)
        if batch_end < new_total:
            time.sleep(float(os.environ.get("EMBED_BATCH_SLEEP", 1)))

    # ------------------------------------------------------------------
    # 7. Final report
    # ------------------------------------------------------------------
    elapsed_total = time.time() - start_time
    info = qdrant.get_collection(collection_name)
    print(
        f"\n[done] Finished in {elapsed_total:.1f}s - "
        f"collection '{collection_name}' has {info.points_count:,} points."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
