"""챗봇 API — Google ADK 에이전트 기반."""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.agents.run_config import RunConfig, StreamingMode
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


import os as _os
import random as _random
from google.genai import Client as _GenaiClient, types as _gentypes

# 추천 질문 캐시 (1시간)
_suggestions_cache: dict = {"questions": [], "ts": 0}


@router.get("/chat/suggestions")
async def chat_suggestions():
    """LLM 기반 동적 추천 질문 3개 생성."""
    import time
    now = time.time()

    # 캐시 유효 (1시간)
    if _suggestions_cache["questions"] and now - _suggestions_cache["ts"] < 3600:
        # 캐시에서 랜덤 3개
        pool = _suggestions_cache["questions"]
        return {"questions": _random.sample(pool, min(3, len(pool)))}

    try:
        client = _GenaiClient(api_key=_os.environ.get("GOOGLE_API_KEY", ""))
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "당신은 북한법 전문 AI 상담 서비스입니다. "
                "사용자가 관심을 가질 만한 흥미롭고 다양한 북한법 관련 질문 8개를 생성하세요. "
                "간결하고 구체적인 질문으로, 각 질문은 다른 주제를 다뤄야 합니다. "
                "JSON 배열로만 응답하세요: [\"질문1\", \"질문2\", ...]"
            ),
            config=_gentypes.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        import json as _json
        questions = _json.loads(resp.text)
        if isinstance(questions, list) and len(questions) >= 3:
            _suggestions_cache["questions"] = questions
            _suggestions_cache["ts"] = now
            return {"questions": _random.sample(questions, 3)}
    except Exception as e:
        logger.warning(f"Suggestions generation failed: {e}")

    # 폴백
    fallback = [
        "북한 과학기술법이 뭐야?",
        "소프트웨어 저작권 관련 법은?",
        "북한 형벌 체계는?",
        "북한에서 외국인 투자 관련 법령은?",
        "북한 헌법의 기본권 조항은?",
    ]
    return {"questions": _random.sample(fallback, 3)}


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
            streamed_any = False  # partial 조각을 사용자에게 보냈는지
            tool_results_buffer: list[str] = []  # 폴백 종합용

            # ADK Runner 실행 — 이벤트 스트리밍
            _run_config = RunConfig(
                streaming_mode=StreamingMode.SSE,
            )

            async for event in _runner.run_async(
                user_id=USER_ID,
                session_id=sid,
                new_message=user_content,
                run_config=_run_config,
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

                # 텍스트 응답 (function_call/response가 없는 이벤트의 텍스트만)
                # ADK StreamingMode.SSE 는 partial=True 조각들을 증분으로 보낸 뒤,
                # 마지막에 partial=False 로 전체 텍스트를 한 번 더 보낸다. 둘 다 yield 하면
                # 답변이 2배로 중복되므로, 조각(partial)만 사용자에게 스트리밍하고
                # 최종 집계 이벤트는 full_response 확정용으로만 쓴다.
                if not fc_list and not fr_list and event.content and event.content.parts:
                    text = "".join(
                        getattr(p, "text", "") or "" for p in event.content.parts
                    )
                    if text:
                        is_partial = bool(getattr(event, "partial", False))
                        if is_partial:
                            streamed_any = True
                            full_response += text
                            yield _sse("token", {"text": text})
                        else:
                            # 최종 집계 이벤트: 전체 텍스트로 확정 (중복 yield 방지)
                            full_response = text
                            if not streamed_any:
                                # 스트리밍 조각이 없었던 경우만 직접 전송
                                yield _sse("token", {"text": text})

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

            # 소스 추출 — DB 법령명과 대조하여 정확한 이름만 반환
            all_sources: list[dict] = []
            seen = set()
            from app.chat.adk_agent.tools import _get_session as _get_db_session
            try:
                from app.core.database import get_session_factory as _gsf
                from sqlalchemy import text as _sql_text
                _factory = _gsf()
                async with _factory() as _sess:
                    _r = await _sess.execute(_sql_text("SELECT name FROM laws"))
                    _law_names = {row["name"] for row in _r.mappings().all()}
            except Exception:
                _law_names = set()

            for name, num in re.findall(r"([\w가-힣,]+(?:법|령))\s*제(\d+)조", full_response):
                clean_name = name.strip().lstrip(",").strip()
                # DB 법령명과 정확히 매칭되는 것만
                if _law_names and clean_name in _law_names:
                    key = (clean_name, num)
                    if key not in seen:
                        seen.add(key)
                        all_sources.append({"law_name": clean_name, "article": num})
                elif not _law_names:
                    # DB 연결 실패 시 폴백
                    key = (clean_name, num)
                    if key not in seen:
                        seen.add(key)
                        all_sources.append({"law_name": clean_name, "article": num})

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
