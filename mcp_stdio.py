from app.core.config import load_config

load_config("config.yaml")

from app.mcp.server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
