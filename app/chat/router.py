"""챗봇 API — 서버 주도 오케스트레이션 에이전트."""
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
from app.chat.orchestrator import (
    classify_query,
    execute_strategy,
    build_synthesis_prompt,
    build_fallback_answer,
    SYNTHESIS_SYSTEM_PROMPT,
    EvidenceBundle,
)
from app.chat.providers.gemini import GeminiProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


@router.post("/chat")
async def chat_endpoint(request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id")

    async def event_stream():
        sid = ""
        try:
            # 1. Session
            sid, messages = chat_session.get_or_create(session_id)
            yield _sse("session", {"session_id": sid})

            # 2. 쿼리 분류 (서버 결정론적, 대화 이력 참고)
            cq = await classify_query(message, session, history=messages)
            yield _sse("thinking", {"text": f"분석 유형: {cq.query_type}"})

            # 3. 검색 전략 실행 (서버 주도, 병렬)
            step_count = 0

            async def on_progress(action: str, detail: str):
                nonlocal step_count
                step_count += 1
                yield_data = {"text": f"{action}: {detail}", "step": step_count}
                # 직접 yield 불가하므로 아래서 처리

            # progress 콜백을 리스트에 수집
            progress_events: list[dict] = []

            async def collect_progress(action: str, detail: str):
                nonlocal step_count
                step_count += 1
                progress_events.append({
                    "action": action,
                    "detail": detail,
                    "step": step_count,
                })

            bundle = await execute_strategy(cq, session, on_progress=collect_progress)

            # 진행 상황 이벤트 전송
            for evt in progress_events:
                yield _sse("tool_call", {
                    "name": evt["action"],
                    "args": {"detail": evt["detail"]},
                    "step": evt["step"],
                })

            # 4. 증거 확인
            if not bundle.evidence_text or bundle.evidence_text.strip() in [
                "검색 결과가 없습니다.",
                "관련 법령을 찾을 수 없습니다.",
                "관련 조문을 찾을 수 없습니다.",
            ]:
                fallback = build_fallback_answer(message, bundle)
                for chunk in _chunk_text(fallback, 80):
                    yield _sse("token", {"text": chunk})
                chat_session.add_message(sid, "user", message)
                chat_session.add_message(sid, "assistant", fallback)
                yield _done(bundle)
                return

            # 5. LLM 종합 답변 생성 (도구 호출 없이 텍스트만)
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            provider = GeminiProvider(api_key=api_key)

            synthesis_prompt = build_synthesis_prompt(message, bundle, history=messages)
            synth_messages = [{"role": "user", "content": synthesis_prompt}]

            yield _sse("thinking", {"text": "답변 작성 중..."})

            full_response = ""
            try:
                async for event in provider.stream_with_tools(
                    synth_messages, [], SYNTHESIS_SYSTEM_PROMPT  # 빈 도구 → 텍스트만
                ):
                    if event["type"] == "token":
                        full_response += event["text"]
                        yield _sse("token", {"text": event["text"]})
            except Exception as e:
                logger.warning(f"LLM synthesis failed: {e}")

            # 6. LLM 실패 시 폴백
            if len(full_response.strip()) < 50:
                logger.info("LLM response too short, using fallback")
                fallback = build_fallback_answer(message, bundle)
                if full_response.strip():
                    fallback = full_response + "\n\n---\n" + fallback
                else:
                    full_response = ""
                for chunk in _chunk_text(fallback, 80):
                    yield _sse("token", {"text": chunk})
                full_response = fallback

            # 7. 세션 저장
            chat_session.add_message(sid, "user", message)
            if full_response:
                chat_session.add_message(sid, "assistant", full_response)

        except Exception as e:
            logger.exception("Chat error")
            yield _sse("error", {"message": f"서버 오류: {str(e)[:200]}"})

        yield _done(bundle if 'bundle' in dir() else EvidenceBundle(query_type="error"))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _done(bundle: EvidenceBundle) -> str:
    """Done 이벤트 — 소스 중복 제거."""
    seen = set()
    unique = []
    for s in bundle.sources:
        key = (s.get("law_name", ""), s.get("article", ""))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return _sse("done", {"sources": unique})


def _chunk_text(text: str, size: int):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
