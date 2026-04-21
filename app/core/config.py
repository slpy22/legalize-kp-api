from __future__ import annotations

import yaml

_config: dict | None = None


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML config from *path* and cache it in the module global."""
    global _config
    with open(path, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    return _config


def get_config() -> dict:
    """Return the cached config, loading the default file if not yet loaded."""
    global _config
    if _config is None:
        load_config()
    return _config
