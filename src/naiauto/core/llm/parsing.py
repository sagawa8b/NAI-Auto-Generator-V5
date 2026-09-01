"""LM Studio 모델 출력 → (프롬프트, 네거티브) 파싱.

시스템 프롬프트로 모델이 `{"prompt": "...", "negative_prompt": "..."}` JSON을
내도록 유도하지만, 로컬 LLM은 그 약속을 자주 어긴다 (설명을 덧붙이거나, 코드펜스로
감싸거나, 그냥 산문으로 답한다). 그래서 파싱은 **절대 깨지지 않고** 최선을 다해
건져낸다 — 실패하면 전체 텍스트를 프롬프트로 본다.

Qt·SDK와 무관한 순수 문자열 처리라 core에 둔다 (테스트가 목 없이 돈다).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

__all__ = ["PromptResult", "parse_prompt_result"]

#: 코드펜스(```json ... ```)를 벗겨내기 위한 패턴. 로컬 모델이 즐겨 감싼다.
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)

#: 프롬프트/네거티브로 인정하는 JSON 키 (모델마다 이름이 조금씩 다르다).
_PROMPT_KEYS = ("prompt", "positive", "positive_prompt", "tags")
_NEGATIVE_KEYS = ("negative_prompt", "negative", "uc", "undesired", "undesired_content")


@dataclass(frozen=True)
class PromptResult:
    """생성된 프롬프트. `raw`는 파싱 전 모델 원문이라 UI가 폴백 표시에 쓸 수 있다."""

    prompt: str
    negative_prompt: str = ""
    raw: str = ""


def parse_prompt_result(raw: str) -> PromptResult:
    """모델 출력에서 프롬프트/네거티브를 뽑는다. 어떤 입력이든 예외를 내지 않는다.

    순서:
    1. 코드펜스 안의 JSON → 전체가 JSON → 문자열 안 첫 JSON 오브젝트 순으로 파싱 시도.
    2. dict에서 알려진 키(대소문자 무시)로 프롬프트/네거티브를 찾는다.
    3. 무엇도 못 찾으면 원문을 통째로 프롬프트로 본다 (코드펜스는 벗겨서).
    """
    text = (raw or "").strip()
    if not text:
        return PromptResult(prompt="", negative_prompt="", raw=raw or "")

    data = _try_parse_json(text)
    if isinstance(data, dict):
        prompt = _first_str(data, _PROMPT_KEYS)
        negative = _first_str(data, _NEGATIVE_KEYS)
        if prompt or negative:
            return PromptResult(prompt=prompt.strip(), negative_prompt=negative.strip(), raw=raw)

    # JSON이 아니거나 알려진 키가 없다 — 코드펜스만 벗기고 통째로 프롬프트로.
    fenced = _FENCE_RE.search(text)
    body = fenced.group(1).strip() if fenced else text
    return PromptResult(prompt=body, negative_prompt="", raw=raw)


def _try_parse_json(text: str) -> object | None:
    """코드펜스 안 → 전체 → 첫 `{...}` 순으로 JSON 파싱을 시도한다. 실패하면 None."""
    fenced = _FENCE_RE.search(text)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)
    brace = _first_brace_object(text)
    if brace is not None:
        candidates.append(brace)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _first_brace_object(text: str) -> str | None:
    """문자열 안의 첫 균형 잡힌 `{...}` 블록. 산문에 JSON이 섞여 온 경우를 건진다."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _first_str(data: dict, keys: tuple[str, ...]) -> str:
    """dict에서 주어진 키(대소문자 무시) 중 처음 나오는 문자열/리스트 값을 문자열로."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        if key in lowered:
            value = lowered[key]
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple)):
                # ["tag1", "tag2"] 형태로 답하는 모델도 있다.
                return ", ".join(str(item).strip() for item in value if str(item).strip())
    return ""
