from __future__ import annotations

import time
from typing import Any


def make_response(
    data: Any,
    start_time: float,
    total: int = 0,
    page: int | None = None,
    per_page: int | None = None,
    total_pages: int | None = None,
) -> dict:
    elapsed = round(time.time() - start_time, 4)
    resp: dict = {
        "ok": True,
        "elapsed": elapsed,
        "total": total,
        "data": data,
    }
    if page is not None:
        resp["page"] = page
    if per_page is not None:
        resp["per_page"] = per_page
    if total_pages is not None:
        resp["total_pages"] = total_pages
    return resp
