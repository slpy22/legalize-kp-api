"""Gemini provider with streaming + function calling + model fallback."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# 모델 우선순위: 고성능 → 경량 폴백
MODEL_PRIORITY = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


class GeminiProvider:
    def __init__(self, api_key: str, model: str | None = None):
        self.client = genai.Client(api_key=api_key)
        self.model = model  # None이면 자동 선택
        self._resolved_model: str | None = None

    async def _resolve_model(self) -> str:
        """사용 가능한 최고 성능 모델을 선택."""
        if self.model:
            return self.model
        if self._resolved_model:
            return self._resolved_model

        loop = asyncio.get_event_loop()
        for model_name in MODEL_PRIORITY:
            try:
                # 간단한 테스트 호출
                def _test():
                    return self.client.models.generate_content(
                        model=model_name,
                        contents="test",
                        config=types.GenerateContentConfig(
                            max_output_tokens=5,
                        ),
                    )
                await loop.run_in_executor(None, _test)
                self._resolved_model = model_name
                logger.info(f"Using model: {model_name}")
                return model_name
            except Exception as e:
                logger.warning(f"Model {model_name} unavailable: {e}")
                continue

        # 모두 실패 시 마지막 모델 사용
        self._resolved_model = MODEL_PRIORITY[-1]
        return self._resolved_model

    async def stream_with_tools(
        self,
        messages: list[dict],
        tool_declarations: list[dict],
        system_prompt: str,
    ) -> AsyncGenerator[dict, None]:
        """Stream response from Gemini with function calling support.

        Yields events:
          {"type": "token", "text": "..."}
          {"type": "tool_call", "name": "...", "args": {...}}
        """
        model = await self._resolve_model()

        # Build Gemini tools
        gemini_tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters=t["parameters"],
                    )
                    for t in tool_declarations
                ]
            )
        ]

        # Convert messages to Gemini content format
        contents = self._build_contents(messages)

        # Stream response
        loop = asyncio.get_event_loop()

        def _start_stream():
            return self.client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=gemini_tools,
                ),
            )

        try:
            response = await loop.run_in_executor(None, _start_stream)
        except Exception as e:
            # 모델 실패 시 폴백
            if model == MODEL_PRIORITY[0] and len(MODEL_PRIORITY) > 1:
                fallback = MODEL_PRIORITY[-1]
                logger.warning(f"{model} failed ({e}), falling back to {fallback}")
                self._resolved_model = fallback

                def _start_fallback():
                    return self.client.models.generate_content_stream(
                        model=fallback,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=gemini_tools,
                        ),
                    )
                response = await loop.run_in_executor(None, _start_fallback)
            else:
                raise

        _SENTINEL = object()

        def _next_or_sentinel(it):
            try:
                return next(it)
            except StopIteration:
                return _SENTINEL

        iterator = iter(response)

        while True:
            chunk = await loop.run_in_executor(None, _next_or_sentinel, iterator)
            if chunk is _SENTINEL:
                break

            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                if part.function_call:
                    fc = part.function_call
                    args = dict(fc.args) if fc.args else {}
                    yield {"type": "tool_call", "name": fc.name, "args": args}
                    return
                elif part.text:
                    yield {"type": "token", "text": part.text}

    @staticmethod
    def _build_contents(messages: list[dict]) -> list:
        """Convert internal message format to Gemini Content objects."""
        contents = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=msg["content"])],
                ))
            elif role == "assistant":
                tc = msg.get("tool_call")
                if tc:
                    contents.append(types.Content(
                        role="model",
                        parts=[types.Part.from_function_call(
                            name=tc["name"],
                            args=tc.get("args", {}),
                        )],
                    ))
                else:
                    text_content = msg.get("content", "")
                    if text_content:
                        contents.append(types.Content(
                            role="model",
                            parts=[types.Part.from_text(text=text_content)],
                        ))
            elif role == "tool":
                tool_data = msg.get("tool_data", {})
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_function_response(
                        name=tool_data.get("name", "unknown"),
                        response={"result": msg["content"]},
                    )],
                ))
        return contents
