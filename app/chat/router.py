from __future__ import annotations

import json
import logging
import os
import re

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

# 중간 사고/계획 텍스트 감지 패턴
_PLANNING_PATTERNS = re.compile(
    r"하겠습니다|살펴보겠|검색하겠|조회하겠|확인하겠|찾아보겠|분석하겠|알아보겠"
    r"|검색$|조회$|실행:|계획:|관찰:"
    r"|키워드로 검색$|도구를 사용"
)


def _is_planning_text(text: str) -> bool:
    """텍스트가 중간 계획/사고 과정인지 판단."""
    if not text.strip():
        return False
    last_chunk = text[-300:]
    return bool(_PLANNING_PATTERNS.search(last_chunk))


def _is_final_answer(text: str, tool_call_count: int) -> bool:
    """텍스트가 최종 답변인지 판단."""
    if tool_call_count == 0:
        return True  # 도구 호출 없이 바로 답변 (간단한 질문)
    # 충분한 길이 + 계획 패턴 없음
    if len(text) > 200 and not _is_planning_text(text):
        return True
    return False


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

            # 4. Provider
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            provider = GeminiProvider(api_key=api_key)

            # 5. Agent loop
            tool_call_count = 0
            empty_rounds = 0
            full_response = ""       # 최종 사용자에게 보이는 응답
            thinking_buffer = ""     # 중간 사고 과정 (사용자에게 안 보임)
            final_answer_started = False

            while tool_call_count < MAX_TOOL_CALLS:
                tool_called = False
                round_text = ""

                async for event in provider.stream_with_tools(
                    messages, TOOL_DECLARATIONS, SYSTEM_PROMPT
                ):
                    if event["type"] == "token":
                        round_text += event["text"]

                        # 도구를 이미 호출한 상태에서 텍스트가 오면 최종 답변 시작
                        if tool_call_count > 0 and not final_answer_started:
                            # 첫 텍스트 — 계획인지 최종 답변인지 판단 보류
                            # 버퍼에 누적하고 나중에 판단
                            pass
                        elif tool_call_count == 0:
                            # 도구 호출 전 — 바로 스트리밍
                            pass

                    elif event["type"] == "tool_call":
                        tool_called = True
                        tool_call_count += 1
                        tool_name = event["name"]
                        tool_args = event["args"]

                        # 이전 라운드 텍스트가 있으면 사고 과정으로 처리
                        if round_text.strip():
                            thinking_buffer += round_text
                            yield _sse("thinking", {"text": round_text.strip()[:200]})
                            round_text = ""

                        yield _sse("tool_call", {
                            "name": tool_name,
                            "args": tool_args,
                            "step": tool_call_count,
                        })

                        messages.append({
                            "role": "assistant",
                            "content": "",
                            "tool_call": {"name": tool_name, "args": tool_args},
                        })

                        result = await execute_tool(tool_name, tool_args, session)
                        all_sources.extend(result.get("sources", []))

                        yield _sse("tool_result", {
                            "name": tool_name,
                            "result": result["result"][:500],
                            "step": tool_call_count,
                        })

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
                    if round_text.strip():
                        # 텍스트만 나온 라운드
                        if _is_planning_text(round_text) and tool_call_count < MAX_TOOL_CALLS:
                            # 중간 계획 → 도구 호출 유도
                            thinking_buffer += round_text
                            yield _sse("thinking", {"text": round_text.strip()[:200]})
                            messages.append({
                                "role": "assistant",
                                "content": round_text,
                            })
                            messages.append({
                                "role": "user",
                                "content": "(계획은 충분합니다. 이제 도구를 호출하여 실제 조문을 조회하세요. 텍스트로 설명하지 말고 도구를 호출하세요.)",
                            })
                            continue
                        else:
                            # 최종 답변
                            full_response += round_text
                            for token in _chunk_text(round_text, 100):
                                yield _sse("token", {"text": token})
                            break
                    else:
                        # 빈 응답
                        empty_rounds += 1
                        if empty_rounds >= 2:
                            break

            # 6. 도구를 호출했지만 최종 답변이 없는 경우 → 강제 종합 요청
            if tool_call_count > 0 and not full_response.strip():
                messages.append({
                    "role": "user",
                    "content": "(조사가 완료되었습니다. 지금까지 조회한 모든 조문 내용을 바탕으로 사용자의 원래 질문에 대해 구체적이고 체계적으로 답변하세요. 반드시 법령명과 조문번호를 인용하며 분석하세요. 중간 계획이나 사고 과정은 출력하지 말고 바로 최종 답변만 하세요.)",
                })
                async for event in provider.stream_with_tools(
                    messages, TOOL_DECLARATIONS, SYSTEM_PROMPT
                ):
                    if event["type"] == "token":
                        full_response += event["text"]
                        yield _sse("token", {"text": event["text"]})
                    elif event["type"] == "tool_call":
                        # 추가 도구 호출도 허용
                        tc_name = event["name"]
                        tc_args = event["args"]
                        messages.append({"role": "assistant", "content": "", "tool_call": {"name": tc_name, "args": tc_args}})
                        result = await execute_tool(tc_name, tc_args, session)
                        all_sources.extend(result.get("sources", []))
                        yield _sse("tool_call", {"name": tc_name, "args": tc_args})
                        yield _sse("tool_result", {"name": tc_name, "result": result["result"][:500]})
                        tc = result["result"]
                        if len(tc) > MAX_TOOL_RESULT_LEN:
                            tc = tc[:MAX_TOOL_RESULT_LEN] + "\n...(이하 생략)"
                        messages.append({"role": "tool", "content": tc, "tool_data": {"name": tc_name}})

            # 7. Save
            if full_response:
                chat_session.add_message(sid, "assistant", full_response)

        except Exception as e:
            logger.exception("Chat error")
            yield _sse("error", {"message": f"서버 오류: {str(e)[:200]}"})

        # 8. Done — 소스 중복 제거
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


def _chunk_text(text: str, size: int):
    """텍스트를 size 단위로 나눠서 yield."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
