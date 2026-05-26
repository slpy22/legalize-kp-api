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

    def get_version_vectors(
        self,
        version_id: int,
        collection: str = "legalize_kp_law_versions",
    ) -> dict[str, list[float]]:
        """주어진 version_id 의 모든 조문 벡터를 {article_number: vector} 로 반환.

        의미적 신구비교(diff_semantic)에서 이미 적재된 임베딩을 재사용하기 위함.
        """
        out: dict[str, list[float]] = {}
        try:
            offset = None
            while True:
                points, offset = self.client.scroll(
                    collection_name=collection,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="version_id",
                                match=models.MatchValue(value=int(version_id)),
                            )
                        ]
                    ),
                    limit=500,
                    with_vectors=True,
                    with_payload=True,
                    offset=offset,
                )
                for p in points:
                    num = (p.payload or {}).get("article_number")
                    if num is not None and p.vector is not None:
                        out[str(num)] = p.vector
                if offset is None:
                    break
        except Exception:
            return {}
        return out

    def search_versions(
        self,
        query_vector: list[float],
        limit: int = 20,
        law_name: str | None = None,
        law_id: int | None = None,
        version_date: str | None = None,
        collection: str = "legalize_kp_law_versions",
    ) -> list[dict]:
        """버전 컬렉션 시맨틱 검색. law_name/law_id/version_date 로 필터.

        시간 축 질의용 — 예: '2010년에 ...에 대한 헌법 조항' 또는 '사회주의헌법 옛 버전 조문 비교'.
        """
        try:
            must: list[models.FieldCondition] = []
            if law_id is not None:
                must.append(
                    models.FieldCondition(
                        key="law_id",
                        match=models.MatchValue(value=int(law_id)),
                    )
                )
            if law_name is not None:
                must.append(
                    models.FieldCondition(
                        key="law_name",
                        match=models.MatchValue(value=law_name),
                    )
                )
            if version_date is not None:
                must.append(
                    models.FieldCondition(
                        key="version_date",
                        match=models.MatchValue(value=version_date),
                    )
                )
            query_filter = models.Filter(must=must) if must else None

            results = self.client.query_points(
                collection_name=collection,
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
            return []
