"""NovelAI 멀티 캐릭터 파이프 문법 — `base | character 1 | character 2`.

NovelAI V4/V5는 프롬프트 안의 맨 `|`로 베이스 프롬프트와 캐릭터 프롬프트를 나눈다
(docs.novelai.net/en/image/multiplecharacters/). 웹UI와 마찬가지로 **캐릭터 프롬프트 칸이
하나라도 채워져 있으면 이 문법은 쓰지 않는다** — 칸이 비어 있을 때만 프롬프트를 쪼갠다.

`||a|b|c||`(공식 Prompt Randomizer) 안쪽의 `|`는 구분자가 아니다. 랜더마이저 구간은 손대지
않고 그대로 NovelAI로 넘긴다.
"""

from __future__ import annotations

__all__ = ["split_pipe_characters"]


def _split_outside_randomizer(text: str) -> list[str]:
    """맨 `|`에서만 자른다. `||`는 랜더마이저 구간 토글이므로 그 안쪽 `|`는 건너뛴다."""
    parts: list[str] = []
    buffer: list[str] = []
    in_randomizer = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "|" and text[index + 1 : index + 2] == "|":
            in_randomizer = not in_randomizer
            buffer.append("||")
            index += 2
            continue
        if char == "|" and not in_randomizer:
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def split_pipe_characters(prompt: str) -> tuple[str, tuple[str, ...]]:
    """`base | c1 | c2`를 (베이스, 캐릭터 프롬프트들)로 나눈다.

    구분자 `|`가 없으면 원본과 빈 튜플을 그대로 돌려준다. 빈 조각(`a || b`가 아니라
    `a | | b`처럼 비어 있는 칸)은 캐릭터로 세지 않는다.
    """
    if "|" not in prompt:
        return prompt, ()
    parts = _split_outside_randomizer(prompt)
    if len(parts) < 2:
        return prompt, ()
    characters = tuple(part.strip() for part in parts[1:] if part.strip())
    if not characters:
        return prompt, ()
    return parts[0].strip(), characters
