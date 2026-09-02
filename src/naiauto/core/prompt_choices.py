"""프롬프트 랜덤 선택 문법 — 꺾쇠 `<a|b|c>` 전용.

V4의 `pickedit_lessthan_str`을 계승해 클라이언트에서 먼저 하나를 뽑아 서버로는 결과만
보낸다. 꺾쇠 `<>`는 NovelAI 문법에 없으므로 충돌하지 않는다.

**bare `|`는 건드리지 않는다.** NovelAI V4/V5에서 맨 `|`는 멀티 캐릭터 프롬프트 구분자이고
(docs.novelai.net/en/image/multiplecharacters/), `||a|b|c||`는 공식 Prompt Randomizer다
(docs.novelai.net/en/image/promptrandomizer/). Prompt Mixing(`promptA|promptB:weight`)은
V3 문법이라 V4 이후로는 존재하지 않는다 — 0.6.0의 "쉼표 구간 bare `a|b` 랜덤"은 이 잘못된
전제 위에 만들어져 두 공식 문법을 모두 깨뜨렸으므로 0.7.2에서 제거했다 (이슈 #4).

문법:
    <a|b|c>   꺾쇠 안에서 하나를 무작위로 선택 (쉼표 포함 조각도 가능, 중첩 지원)
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


def resolve_prompt_choices(text: str, rng: random.Random) -> str:
    """`<a|b|c>`에서 하나씩 무작위로 선택해 치환한다. 그 밖의 `|`는 그대로 둔다."""
    if "|" not in text:
        return text
    return _resolve_bracket_choices(text, rng)


__all__ = ["resolve_prompt_choices"]
