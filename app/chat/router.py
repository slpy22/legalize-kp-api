"""챗봇 API — Google ADK 에이전트 기반."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote

import httpx
import websockets
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


# ---------------------------------------------------------------------------
# 자체 에이전트(self) — Claude Code Session Manager 브리지
#
# select 에서 "자체 에이전트"(llm="self") 선택 시, Gemini/ADK 대신 로컬 호스트에
# 떠 있는 Session Manager(기본 5100)의 라벨 세션을 재개해 처리한다.
#   1) GET  {SM_BASE}/api/v1/sessions?label=<라벨>  → session_id 해석
#   2) WS   {SM_WS}/api/v1/resume/{id}?token=..&skip=1 → prompt 1턴 전송
#   3) claude stream-json 이벤트(assistant/result)를 기존 SSE(token/tool_call/done)로 변환
#
# 컨테이너에서 호스트로 나가므로 기본 베이스는 host.docker.internal 이고,
# 비루프백 요청이라 SM_API_TOKEN 이 필요하다(Session Manager 측도 토큰 설정 필수).
# ---------------------------------------------------------------------------
SM_BASE = os.environ.get("SM_RESUME_BASE", "http://host.docker.internal:5100").rstrip("/")
SM_TOKEN = os.environ.get("SM_API_TOKEN", "")
SM_LABEL = os.environ.get("SM_AGENT_LABEL", "북한법전문가")


def _sm_ws_base() -> str:
    if SM_BASE.startswith("https://"):
        return "wss://" + SM_BASE[len("https://"):]
    if SM_BASE.startswith("http://"):
        return "ws://" + SM_BASE[len("http://"):]
    return "ws://" + SM_BASE


async def _sm_find_session_id() -> str | None:
    """SM_LABEL 을 라벨(tags) 또는 표시이름(name)으로 갖는 세션의 id 를 반환.

    SM 은 라벨(tags 배열)과 표시이름(name)이 별개다. 먼저 라벨 필터로 조회하고,
    없으면 전체 목록에서 표시이름이 일치하는 가장 최근 세션을 찾는다.
    (사용자는 둘 중 무엇으로 지정했든 'SM_AGENT_LABEL' 값으로 인식한다.)
    """
    headers = {"Authorization": f"Bearer {SM_TOKEN}"} if SM_TOKEN else {}
    async with httpx.AsyncClient(timeout=15) as client:
        # 1) 라벨(tags) 매칭
        r = await client.get(
            f"{SM_BASE}/api/v1/sessions",
            params={"label": SM_LABEL, "limit": 1},
            headers=headers,
        )
        r.raise_for_status()
        sessions = r.json().get("sessions", [])
        if sessions:
            return sessions[0]["session_id"]

        # 2) 표시이름(name) 매칭 폴백 (목록은 ended_at 최신순 정렬돼 옴)
        r = await client.get(
            f"{SM_BASE}/api/v1/sessions",
            params={"limit": 0},
            headers=headers,
        )
        r.raise_for_status()
        for s in r.json().get("sessions", []):
            if s.get("label_name") == SM_LABEL or SM_LABEL in (s.get("labels") or []):
                return s["session_id"]
    return None


def _extract_sources_simple(text: str) -> list[dict]:
    """응답 텍스트에서 '○○법 제N조' 인용을 추출(중복 제거)."""
    out: list[dict] = []
    seen: set = set()
    for name, num in re.findall(r"([\w가-힣,]+(?:법|령))\s*제(\d+)조", text):
        clean = name.strip().lstrip(",").strip()
        key = (clean, num)
        if key not in seen:
            seen.add(key)
            out.append({"law_name": clean, "article": num})
    return out


def _extract_sources_self(text: str, queried_laws: list[str]) -> list[dict]:
    """자체 에이전트 답변의 참고 조문 추출.

    자체 에이전트는 '헌법 ... 제62조' 처럼 법령명과 조번호를 떨어뜨려 쓰므로
    인접 패턴만으로는 못 잡는다. 대신 MCP 로 조회한 실제 DB 법령명(queried_laws)을
    기준으로, 본문의 '제N조'를 가장 가까운(직전) 법령 문맥에 연결한다.
    단일 법령만 조회했으면 전부 그 법령에 귀속. 조회 법령이 없으면 인접 패턴 폴백.
    """
    if not queried_laws:
        return _extract_sources_simple(text)

    # 실제 DB명 + 짧은형(마지막 어절, 예: "조선…민주주의…헌법"→"헌법") 매핑
    smap: dict = {}
    for db in queried_laws:
        for form in {db, db.split()[-1]}:
            if form:
                smap[form] = db
    forms = sorted(smap.keys(), key=len, reverse=True)
    name_alt = "|".join(re.escape(f) for f in forms)
    tok = re.compile(rf"({name_alt})|제\s*(\d+)\s*조")

    seen: set = set()
    out: list[dict] = []
    current = queried_laws[0] if len(queried_laws) == 1 else None
    for m in tok.finditer(text):
        if m.group(1):
            current = smap.get(m.group(1), current)
        elif m.group(2) and current:
            key = (current, m.group(2))
            if key not in seen:
                seen.add(key)
                out.append({"law_name": current, "article": m.group(2)})
    return out


async def _stream_self_agent(message: str, prev_session_id: str | None):
    """자체 에이전트(SM 세션) 경로 — SSE 문자열을 순차 yield 한다."""
    # 1) 대상 세션 해석
    try:
        sid = await _sm_find_session_id()
    except Exception as e:  # noqa: BLE001
        logger.warning("SM session lookup failed: %s", e)
        yield _sse("error", {"message": f"세션 매니저 연결 실패: {str(e)[:180]}"})
        yield _sse("done", {"sources": [], "session_id": prev_session_id or ""})
        return

    if not sid:
        yield _sse("error", {"message": f"'{SM_LABEL}' 라벨/이름의 세션을 찾을 수 없습니다. SM에서 세션을 만들고 라벨 또는 이름을 지정하세요."})
        yield _sse("done", {"sources": [], "session_id": prev_session_id or ""})
        return

    yield _sse("session", {"session_id": sid})
    yield _sse("thinking", {"text": "자체 에이전트에 연결하는 중..."})

    # 2) WS 재개 + 프롬프트 전송
    tok = f"&token={quote(SM_TOKEN)}" if SM_TOKEN else ""
    url = f"{_sm_ws_base()}/api/v1/resume/{sid}?skip=1{tok}"
    full_text = ""
    step = 0
    queried_laws: list[str] = []  # MCP로 조회한 실제 DB 법령명(참고링크 근거)
    try:
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if first.get("type") == "error":
                yield _sse("error", {"message": first.get("message", "세션 재개에 실패했습니다.")})
                yield _sse("done", {"sources": [], "session_id": sid})
                return

            await ws.send(json.dumps({"type": "prompt", "text": message}, ensure_ascii=False))

            # 3) claude stream-json 이벤트 중계
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=300)
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    # claude 가 stdout 에 섞어 보낸 비-JSON 라인(경고/로그 등) 무시
                    continue
                t = ev.get("type")
                if t == "assistant":
                    for b in ev.get("message", {}).get("content", []):
                        bt = b.get("type")
                        if bt == "text":
                            txt = b.get("text", "") or ""
                            if txt:
                                full_text += txt
                                yield _sse("token", {"text": txt})
                        elif bt == "tool_use":
                            step += 1
                            nm = b.get("name", "tool")
                            _inp = b.get("input") or {}
                            _lname = _inp.get("name")
                            if _lname and nm in (
                                "mcp__nklaw__law_get", "mcp__nklaw__law_history",
                                "mcp__nklaw__law_diff", "mcp__nklaw__tools_verify",
                            ):
                                queried_laws.append(str(_lname))
                            yield _sse("tool_call", {"name": nm, "step": step})
                elif t == "result":
                    if not full_text.strip():
                        rt = ev.get("result") or ""
                        if rt:
                            full_text = rt
                            yield _sse("token", {"text": rt})
                    break
                elif t in ("error", "closed"):
                    if not full_text.strip():
                        yield _sse("error", {"message": ev.get("message") or "에이전트 연결이 종료되었습니다."})
                    break
    except Exception as e:  # noqa: BLE001
        logger.exception("Self-agent bridge error")
        if not full_text.strip():
            yield _sse("error", {"message": f"자체 에이전트 오류: {str(e)[:180]}"})
        yield _sse("done", {"sources": [], "session_id": sid})
        return

    _uniq_laws = list(dict.fromkeys(queried_laws))
    yield _sse("done", {"sources": _extract_sources_self(full_text, _uniq_laws), "session_id": sid})


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
    llm = body.get("llm", "gemini")

    async def event_stream():
        # 자체 에이전트 경로 — Gemini/ADK 대신 Session Manager 세션으로 중계
        if llm == "self":
            async for chunk in _stream_self_agent(message, session_id):
                yield chunk
            return

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
