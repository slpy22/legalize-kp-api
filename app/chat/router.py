from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.chat import session as chat_session
from app.chat.term_converter import expand_query
from app.chat.tools import TOOL_DECLARATIONS, SYSTEM_PROMPT, execute_tool
from app.chat.providers.gemini import GeminiProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

MAX_TOOL_CALLS = 15
MAX_TOOL_RESULT_LEN = 4000
MAX_EMPTY_ROUNDS = 2  # 연속 빈 응답 허용 횟수


@router.post("/chat")
async def chat_endpoint(request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id")

    async def event_stream():
        all_sources: list[dict] = []
        sid = ""
        try:
            # 1. Session
            sid, messages = chat_session.get_or_create(session_id)
            yield _sse("session", {"session_id": sid})

            # 2. Expand query
            expanded_message = expand_query(message)

            # 3. Add user message
            chat_session.add_message(sid, "user", expanded_message)
            messages = chat_session.get_or_create(sid)[1]

            # 4. Provider (자동 모델 선택 + 폴백)
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            provider = GeminiProvider(api_key=api_key)

            # 5. Agent loop — ReAct 패턴
            tool_call_count = 0
            empty_rounds = 0
            full_response = ""

            while tool_call_count < MAX_TOOL_CALLS:
                tool_called = False
                round_has_text = False

                async for event in provider.stream_with_tools(
                    messages, TOOL_DECLARATIONS, SYSTEM_PROMPT
                ):
                    if event["type"] == "token":
                        full_response += event["text"]
                        round_has_text = True
                        yield _sse("token", {"text": event["text"]})

                    elif event["type"] == "tool_call":
                        tool_called = True
                        tool_call_count += 1
                        tool_name = event["name"]
                        tool_args = event["args"]

                        yield _sse("tool_call", {
                            "name": tool_name,
                            "args": tool_args,
                            "step": tool_call_count,
                        })

                        # Assistant function_call → messages
                        messages.append({
                            "role": "assistant",
                            "content": "",
                            "tool_call": {"name": tool_name, "args": tool_args},
                        })

                        # Execute tool
                        result = await execute_tool(tool_name, tool_args, session)
                        all_sources.extend(result.get("sources", []))

                        yield _sse("tool_result", {
                            "name": tool_name,
                            "result": result["result"][:500],
                            "step": tool_call_count,
                        })

                        # Tool result → messages (크기 제한)
                        tool_content = result["result"]
                        if len(tool_content) > MAX_TOOL_RESULT_LEN:
                            tool_content = tool_content[:MAX_TOOL_RESULT_LEN] + "\n...(이하 생략)"
                        messages.append({
                            "role": "tool",
                            "content": tool_content,
                            "tool_data": {"name": tool_name},
                        })
                        break

                if not tool_called:
                    if round_has_text:
                        # 텍스트에 "~하겠습니다"가 포함되면 아직 계획 단계 → 도구 호출 유도
                        last_text = full_response[-200:] if full_response else ""
                        is_planning = any(kw in last_text for kw in [
                            "하겠습니다", "살펴보겠", "검색하겠", "조회하겠",
                            "확인하겠", "찾아보겠", "분석하겠", "알아보겠",
                        ])
                        if is_planning and tool_call_count < MAX_TOOL_CALLS:
                            # 계획 텍스트 후 실제 도구 호출 유도
                            messages.append({
                                "role": "assistant",
                                "content": full_response,
                            })
                            messages.append({
                                "role": "user",
                                "content": "(계속 진행하세요. 말로 설명하지 말고 도구를 호출하세요.)",
                            })
                            continue
                        # 최종 답변
                        break
                    else:
                        # 빈 응답 — 재시도 (LLM이 가끔 빈 응답 반환)
                        empty_rounds += 1
                        if empty_rounds >= MAX_EMPTY_ROUNDS:
                            # 재시도 초과 — 강제 응답 요청
                            messages.append({
                                "role": "user",
                                "content": "(시스템: 지금까지 조회한 내용을 바탕으로 사용자의 질문에 답변해 주세요. 조문을 인용하며 구체적으로 답변하세요.)",
                            })
                            # 마지막 시도
                            async for event in provider.stream_with_tools(
                                messages, TOOL_DECLARATIONS, SYSTEM_PROMPT
                            ):
                                if event["type"] == "token":
                                    full_response += event["text"]
                                    yield _sse("token", {"text": event["text"]})
                            break

            # 6. Save
            if full_response:
                chat_session.add_message(sid, "assistant", full_response)

        except Exception as e:
            logger.exception("Chat error")
            yield _sse("error", {"message": f"서버 오류: {str(e)[:200]}"})

        # 7. Done
        # 소스 중복 제거
        seen = set()
        unique_sources = []
        for s in all_sources:
            key = (s.get("law_name", ""), s.get("article", ""))
            if key not in seen:
                seen.add(key)
                unique_sources.append(s)

        yield _sse("done", {"sources": unique_sources, "session_id": sid})

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
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
