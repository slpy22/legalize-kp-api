from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models


class QdrantSearchRepository:
    def __init__(self, client: QdrantClient, collection: str):
        self.client = client
        self.collection = collection

    def search(
        self,
        query_vector: list[float],
        limit: int = 20,
        category: str | None = None,
    ) -> list[dict]:
        try:
            query_filter = None
            if category is not None:
                query_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key="category",
                            match=models.MatchValue(value=category),
                        )
                    ]
                )

            results = self.client.query_points(
                collection_name=self.collection,
                query=query_vector,
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )
            return [
                {
                    "id": str(point.id),
                    "score": point.score,
                    **(point.payload or {}),
                }
                for point in results.points
            ]
        except Exception:
            # Collection may not exist yet (embeddings still building)
            return []
