"""챗봇 API — Google ADK 에이전트 기반."""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from app.chat.adk_agent.agent import nk_law_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

# ADK 세션 서비스 + Runner (싱글톤)
_session_service = InMemorySessionService()
_runner = Runner(
    agent=nk_law_agent,
    app_name="nk_law_chat",
    session_service=_session_service,
)

APP_NAME = "nk_law_chat"
USER_ID = "web_user"


@router.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id")

    async def event_stream():
        sid = session_id or ""
        try:
            # 세션 가져오기 또는 생성
            if sid:
                session = await _session_service.get_session(
                    app_name=APP_NAME, user_id=USER_ID, session_id=sid
                )
                if not session:
                    session = await _session_service.create_session(
                        app_name=APP_NAME, user_id=USER_ID, session_id=sid
                    )
            else:
                session = await _session_service.create_session(
                    app_name=APP_NAME, user_id=USER_ID
                )
            sid = session.id
            yield _sse("session", {"session_id": sid})

            # ADK 사용자 메시지
            user_content = genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=message)],
            )

            full_response = ""
            step_count = 0
            tool_results_buffer: list[str] = []  # 폴백 종합용

            # ADK Runner 실행 — 이벤트 스트리밍
            async for event in _runner.run_async(
                user_id=USER_ID,
                session_id=sid,
                new_message=user_content,
            ):
                # 도구 호출 (function_call)
                fc_list = event.get_function_calls()
                if fc_list:
                    for call in fc_list:
                        step_count += 1
                        yield _sse("tool_call", {
                            "name": call.name,
                            "args": dict(call.args) if call.args else {},
                            "step": step_count,
                        })

                # 도구 응답 (function_response)
                fr_list = event.get_function_responses()
                if fr_list:
                    for resp in fr_list:
                        result_str = ""
                        if resp.response:
                            full_result = resp.response.get("result", "") if isinstance(resp.response, dict) else str(resp.response)
                            result_str = str(full_result)[:500]
                            tool_results_buffer.append(f"[{resp.name}]\n{full_result}")
                        yield _sse("tool_result", {
                            "name": resp.name,
                            "result": result_str,
                            "step": step_count,
                        })

                # 텍스트 응답 (최종 답변 — function_call/response가 없는 이벤트의 텍스트만)
                if not fc_list and not fr_list and event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, 'text') and part.text:
                            full_response += part.text
                            yield _sse("token", {"text": part.text})

            # 빈 응답 폴백: ADK가 텍스트를 생성하지 못한 경우
            # → 세션에서 tool result를 추출하여 별도 LLM 종합 호출
            if not full_response.strip() and step_count > 0 and tool_results_buffer:
                yield _sse("thinking", {"text": "답변 작성 중..."})
                full_response = await _fallback_synthesis_direct(message, tool_results_buffer)
                if full_response:
                    yield _sse("token", {"text": full_response})
                else:
                    fallback = "도구 조회는 완료되었으나 답변 생성에 실패했습니다. 더 구체적으로 질문해 주세요."
                    yield _sse("token", {"text": fallback})
                    full_response = fallback

            # 소스 추출 (응답에서 법령명 제N조 패턴)
            all_sources: list[dict] = []
            seen = set()
            for name, num in re.findall(r"([\w가-힣,\s]+?(?:법|령))\s*제(\d+)조", full_response):
                key = (name.strip(), num)
                if key not in seen:
                    seen.add(key)
                    all_sources.append({"law_name": name.strip(), "article": num})

        except Exception as e:
            logger.exception("Chat error")
            yield _sse("error", {"message": f"서버 오류: {str(e)[:200]}"})
            all_sources = []
            sid = sid or ""

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


async def _fallback_synthesis_direct(user_query: str, tool_results: list[str]) -> str:
    """tool_results 버퍼에서 직접 LLM 종합."""
    import os
    from google.genai import Client, types

    evidence = "\n\n".join(tool_results)
    if len(evidence) > 10000:
        evidence = evidence[:10000] + "\n...(이하 생략)"

    prompt = f"""사용자 질문: {user_query}

## 조사 결과

{evidence}

---
위 조사 결과를 바탕으로 사용자 질문에 체계적이고 구체적으로 답변하세요.
반드시 법령명과 조문번호를 인용하세요. 마크다운으로 작성하세요."""

    try:
        client = Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="당신은 북한법 전문 분석가입니다. 제공된 조사 결과만 바탕으로 답변하세요. 중간 과정 없이 바로 최종 답변만 작성하세요.",
            ),
        )
        return response.text or ""
    except Exception as e:
        logger.warning(f"Fallback synthesis failed: {e}")
        return ""


async def _fallback_synthesis(user_query: str, session_id: str) -> str:
    """ADK 에이전트가 빈 응답을 줄 때 세션에서 도구 결과를 추출하여 별도 LLM 종합."""
    import os
    from google.genai import Client, types

    # 세션에서 도구 결과 추출
    session = await _session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if not session or not session.events:
        return ""

    tool_results = []
    for event in session.events:
        fr_list = event.get_function_responses()
        if fr_list:
            for resp in fr_list:
                if resp.response:
                    result_text = resp.response.get("result", "") if isinstance(resp.response, dict) else str(resp.response)
                    tool_results.append(f"[{resp.name}]\n{result_text}")

    if not tool_results:
        return ""

    evidence = "\n\n".join(tool_results)
    if len(evidence) > 10000:
        evidence = evidence[:10000] + "\n...(이하 생략)"

    prompt = f"""사용자 질문: {user_query}

## 조사 결과

{evidence}

---
위 조사 결과를 바탕으로 사용자 질문에 체계적이고 구체적으로 답변하세요.
반드시 법령명과 조문번호를 인용하세요.
마크다운으로 작성하세요.
중간 계획이나 사고 과정은 출력하지 마세요."""

    try:
        client = Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="당신은 북한법 전문 분석가입니다. 제공된 조사 결과만 바탕으로 답변하세요.",
            ),
        )
        return response.text or ""
    except Exception as e:
        logger.warning(f"Fallback synthesis failed: {e}")
        return ""


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
