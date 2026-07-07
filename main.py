from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import load_config

# .env 파일에서 환경변수 자동 로드
load_dotenv(Path(__file__).parent / ".env")
from app.api.router import router
from app.compare.router import router as compare_router
from app.mcp.server import mcp
from app.chat.router import router as chat_router


def create_app():
    load_config("config.yaml")

    # streamable-http MCP 앱 생성 + 세션매니저 lifespan 배선.
    # mount 만 하면 session_manager task group 이 시작되지 않아
    # "Task group is not initialized" 500 이 난다 → lifespan 으로 run() 실행.
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="legalize-kp API", version="1.0.0", lifespan=lifespan)

    # CORS 허용 (웹 프론트엔드에서 API 호출)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(compare_router)
    app.include_router(chat_router)
    app.mount("/mcp", mcp_app)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "main:app",
        host=cfg["server"]["host"],
        port=cfg["server"]["port"],
        reload=False,
    )
