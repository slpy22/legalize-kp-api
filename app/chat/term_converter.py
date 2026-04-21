from __future__ import annotations

import json
from pathlib import Path

_TERM_PAIRS_PATH = Path("E:/004_북한법/legalize-kp/compare/term_pairs.json")

# Build reverse map: kr -> kp
_kr_to_kp: dict[str, str] = {}

try:
    with open(_TERM_PAIRS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    for item in data.get("terms", []):
        kr = item.get("kr", "")
        kp = item.get("kp", "")
        if kr and kp:
            _kr_to_kp[kr] = kp
except (FileNotFoundError, json.JSONDecodeError):
    pass


def expand_query(query: str) -> str:
    """남한어를 문화어로 변환하고, 원본도 유지하여 검색 범위를 넓힌다.

    "소프트웨어 저작권" → "쏘프트웨어 저작권"  (치환)
    또한 개별 단어 검색도 가능하도록 분리된 단어 목록도 반환.
    """
    converted = query
    for kr, kp in _kr_to_kp.items():
        if kr in converted:
            converted = converted.replace(kr, kp)

    # 원본과 변환본이 다르면 둘 다 포함 (OR 검색 효과)
    if converted != query:
        return f"{converted} {query}"
    return query
