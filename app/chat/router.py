from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.chat import session as chat_session
from app.chat.term_converter import expand_query
from app.chat.tools import TOOL_DECLARATIONS, SYSTEM_PROMPT, execute_tool
from app.chat.providers.gemini import GeminiProvider

router = APIRouter(prefix="/api/v1")

MAX_TOOL_CALLS = 10


@router.post("/chat")
async def chat_endpoint(request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id")
    llm = body.get("llm", "gemini")

    async def event_stream():
        all_sources: list[dict] = []
        sid = ""
        try:
            # 1. Get/create chat session
            sid, messages = chat_session.get_or_create(session_id)
            yield _sse("session", {"session_id": sid})

            # 2. Expand query with term converter
            expanded_message = expand_query(message)

            # 3. Add user message to session
            chat_session.add_message(sid, "user", expanded_message)
            messages = chat_session.get_or_create(sid)[1]

            # 4. Create provider
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            provider = GeminiProvider(api_key=api_key)

            # 5. Agent loop
            tool_call_count = 0
            full_response = ""

            while tool_call_count < MAX_TOOL_CALLS:
                tool_called = False

                async for event in provider.stream_with_tools(
                    messages, TOOL_DECLARATIONS, SYSTEM_PROMPT
                ):
                    if event["type"] == "token":
                        full_response += event["text"]
                        yield _sse("token", {"text": event["text"]})

                    elif event["type"] == "tool_call":
                        tool_called = True
                        tool_call_count += 1
                        tool_name = event["name"]
                        tool_args = event["args"]

                        yield _sse("tool_call", {"name": tool_name, "args": tool_args})

                        # assistant의 function_call 메시지를 이력에 추가
                        messages.append({
                            "role": "assistant",
                            "content": "",
                            "tool_call": {"name": tool_name, "args": tool_args},
                        })

                        result = await execute_tool(tool_name, tool_args, session)
                        all_sources.extend(result.get("sources", []))

                        yield _sse("tool_result", {"name": tool_name, "result": result["result"][:500]})

                        messages.append({
                            "role": "tool",
                            "content": result["result"],
                            "tool_data": {"name": tool_name},
                        })
                        break

                if not tool_called:
                    break

            # 6. Save assistant response
            if full_response:
                chat_session.add_message(sid, "assistant", full_response)

        except Exception as e:
            yield _sse("error", {"message": f"서버 오류: {str(e)[:200]}"})

        # 7. Final done event (항상 전송 — 클라이언트가 스트림 종료를 감지)
        yield _sse("done", {"sources": all_sources, "session_id": sid})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
