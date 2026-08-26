"""프롬프트 랜덤 선택 문법 — `<a|b|c>` 및 쉼표 구간의 bare `a|b`.

NovelAI 자체 `|` 문법은 "Prompt Mixing"(`promptA|promptB:weight` — 두 프롬프트를 섞음,
docs.novelai.net/en/image/promptmixing/)이며 랜덤 선택이 아니다. 이 모듈은 V4의
`pickedit_lessthan_str`을 계승해 클라이언트에서 먼저 하나를 뽑아 서버로는 결과만 보낸다
— 그래서 이 처리를 거친 뒤에는 순수 NovelAI 믹싱 문법을 그대로 쓸 여지가 없다(의도된
트레이드오프. 기능 요청 시 사용자와 확인됨).

문법:
    <a|b|c>   꺾쇠 안에서 하나를 무작위로 선택 (쉼표 포함 조각도 가능, 중첩 지원)
    a|b|c     쉼표로 나눈 한 구간 안에 그대로 있으면 그 구간에서 하나를 선택
"""

from __future__ import annotations

import random

MAX_TRY_AMOUNT = 10


def _resolve_bracket_choices(text: str, rng: random.Random) -> str:
    """`<a|b|c>`를 무작위로 하나 선택해 치환. 안쪽 것부터(중첩 지원), 최대 MAX_TRY_AMOUNT 패스."""
    result = text
    for _pass in range(MAX_TRY_AMOUNT):
        changed = False
        prev_point = 0
        while True:
            pos_r = result.find(">", prev_point)
            if pos_r == -1:
                break
            pos_l = result.rfind("<", prev_point, pos_r)
            if pos_l == -1:
                prev_point = pos_r + 1
                continue
            center = result[pos_l + 1 : pos_r]
            if "|" not in center:
                prev_point = pos_r + 1
                continue
            picked = rng.choice(center.split("|"))
            result = result[:pos_l] + picked + result[pos_r + 1 :]
            prev_point = pos_l + len(picked)
            changed = True
        if not changed:
            break
    return result


def _resolve_comma_segment_choices(text: str, rng: random.Random) -> str:
    """쉼표로 나눈 구간에 `|`가 남아 있으면 그 구간에서 하나를 무작위 선택."""
    segments = text.split(",")
    resolved: list[str] = []
    for segment in segments:
        if "|" not in segment:
            resolved.append(segment)
            continue
        leading = segment[: len(segment) - len(segment.lstrip())]
        trailing = segment[len(segment.rstrip()) :]
        core = segment.strip()
        picked = rng.choice([choice.strip() for choice in core.split("|")])
        resolved.append(f"{leading}{picked}{trailing}")
    return ",".join(resolved)


def resolve_prompt_choices(text: str, rng: random.Random) -> str:
    """`<a|b|c>` 및 쉼표 구간의 `a|b`를 각각 하나씩 무작위로 선택해 치환한다."""
    if "|" not in text:
        return text
    result = _resolve_bracket_choices(text, rng)
    result = _resolve_comma_segment_choices(result, rng)
    return result


__all__ = ["resolve_prompt_choices"]
