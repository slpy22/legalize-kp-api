from __future__ import annotations

import json
from pathlib import Path

# Lazy-loaded reverse map: kr -> kp
_kr_to_kp: dict[str, str] | None = None


def _load_terms() -> dict[str, str]:
    """config의 compare_path(없으면 repo_path/compare)에서 term_pairs.json을 로드."""
    from app.core.config import get_config

    cfg = get_config()
    data_cfg = cfg.get("data", {})
    compare_dir = data_cfg.get("compare_path") or str(
        Path(data_cfg.get("repo_path", ".")) / "compare"
    )
    term_path = Path(compare_dir) / "term_pairs.json"

    mapping: dict[str, str] = {}
    try:
        with open(term_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("terms", []):
            kr = item.get("kr", "")
            kp = item.get("kp", "")
            if kr and kp:
                mapping[kr] = kp
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return mapping


def _get_terms() -> dict[str, str]:
    global _kr_to_kp
    if _kr_to_kp is None:
        _kr_to_kp = _load_terms()
    return _kr_to_kp


def expand_query(query: str) -> str:
    """남한어를 문화어로 변환하고, 원본도 유지하여 검색 범위를 넓힌다.

    "소프트웨어 저작권" → "쏘프트웨어 저작권"  (치환)
    """
    terms = _get_terms()
    converted = query
    for kr, kp in terms.items():
        if kr in converted:
            converted = converted.replace(kr, kp)

    if converted != query:
        return f"{converted} {query}"
    return query
