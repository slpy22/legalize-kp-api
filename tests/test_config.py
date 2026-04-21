from app.core.config import load_config


def test_load_config():
    cfg = load_config("config.yaml")
    assert cfg["server"]["port"] == 8000
    assert cfg["database"]["database"] == "legalize_kp"
    assert cfg["qdrant"]["collection"] == "legalize_kp_laws"
