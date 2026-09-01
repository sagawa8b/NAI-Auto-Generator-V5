"""프롬프트 안의 화면 비율 지시어 — `<res:wide|square|portrait>` (Qt-free core).

프롬프트에 `<res:portrait>`를 쓰면 그 장만 세로형 해상도로 뽑는다. 와일드카드 항목에
넣어 두면 장마다 비율이 달라진다 — 해상도 랜덤이 등급 안에서 아무거나 고르는 것과 달리
"이 프롬프트는 세로"처럼 내용과 비율을 묶을 수 있다.

지시어는 NovelAI로 나가기 전에 프롬프트에서 지운다. `prompt_choices`의 `<a|b>`와 문법이
겹쳐 보이지만 그쪽은 `|`가 있을 때만 동작하므로 서로 건드리지 않는다. 다만 선택이 끝난
뒤의 결과에서 지시어를 찾아야 하므로, 호출 순서는 와일드카드 → 아티스트 조합 →
프롬프트 선택 → **이 모듈** 이다.

원안: aliceknowing (공개 저장소 PR #1).
"""

from __future__ import annotations

import re

from .resolution_catalog import Aspect

DIRECTIVE_RE = re.compile(r"<res:(wide|square|portrait)>", re.IGNORECASE)

_REPEATED_COMMA_RE = re.compile(r",(?:\s*,)+")
_RUN_OF_SPACES_RE = re.compile(r"[^\S\r\n]{2,}")


def extract_resolution_directive(prompt: str) -> tuple[str, Aspect | None]:
    """지시어를 지운 프롬프트와, 마지막으로 지정된 Aspect를 돌려준다.

    지시어가 없으면 프롬프트를 그대로 돌려준다 (`(prompt, None)`). 여러 개가 있으면
    **마지막** 것을 쓰고 전부 지운다. `<res:banana>` 같은 미지의 값은 지시어가 아니므로
    손대지 않는다 — 오타를 조용히 삼키는 것보다 프롬프트에 남아 눈에 띄는 편이 낫다.
    """
    matches = list(DIRECTIVE_RE.finditer(prompt))
    if not matches:
        return prompt, None

    aspect = Aspect(matches[-1].group(1).capitalize())
    cleaned = DIRECTIVE_RE.sub("", prompt)
    # 쉼표로 구분된 지시어를 지우면 빈 구간이 남는다 (`a, <res:wide>, b` → `a, , b`).
    cleaned = _REPEATED_COMMA_RE.sub(",", cleaned)
    cleaned = cleaned.strip().strip(",").strip()
    cleaned = _RUN_OF_SPACES_RE.sub(" ", cleaned)
    return cleaned, aspect


__all__ = ["DIRECTIVE_RE", "extract_resolution_directive"]
