"""Gemini provider with streaming + function calling."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from google import genai
from google.genai import types


class GeminiProvider:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite"):
        self.client = genai.Client(api_key=api_key)
        self.model = model

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
        contents = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=msg["content"])]))
            elif role == "assistant":
                tc = msg.get("tool_call")
                if tc:
                    # assistant가 function_call을 한 경우
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
                        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text_content)]))
            elif role == "tool":
                tool_data = msg.get("tool_data", {})
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=tool_data.get("name", "unknown"),
                                response={"result": msg["content"]},
                            )
                        ],
                    )
                )

        # Stream response — sync iterator를 async로 변환
        response = self.client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=gemini_tools,
            ),
        )

        # sync iterator를 async로 변환
        # next()의 StopIteration은 run_in_executor에서 RuntimeError로 변환되므로
        # sentinel 패턴을 사용
        _SENTINEL = object()

        def _next_or_sentinel(it):
            try:
                return next(it)
            except StopIteration:
                return _SENTINEL

        loop = asyncio.get_event_loop()
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
