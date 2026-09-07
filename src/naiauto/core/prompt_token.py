"""커서 자리에서 "지금 무엇을 치고 있는가"를 읽어 낸다 — 자동완성이 쓰는 토큰 분해.

프롬프트 한 칸에는 태그와 와일드카드가 섞여 있고, 자동완성은 커서 **바로 앞**에 있는 것 하나만
보면 된다. 그 하나를 골라 내는 규칙을 여기 모았다 (Qt 없이 테스트할 수 있게 core에 둔다).

두 가지를 본다.

1. **구분자** — 쉼표뿐 아니라 **줄바꿈**과 `|`(캐릭터 구분자)도 토큰을 끊는다. 프롬프트를
   여러 줄로 쓰는 사람이 많은데 쉼표만 보면 앞 줄까지 한 토큰이 되어 아무것도 안 걸린다.
2. **와일드카드 여는 기호** — `__` · `__=` · `##`는 그 자체가 토큰의 시작이다. 빈 칸에서만
   되면 곤란하다: `1girl, blue hair __hai`처럼 **쓰던 줄 도중에** 쳐도 목록이 떠야 한다
   (v0.7.8에서 메인 프롬프트가 이것 때문에 안 떴다 — 캐릭터 슬롯은 비어 있어 우연히 됐다).
   이미 닫힌 와일드카드(`__hair__`)는 건너뛰고 그 **뒤**부터 다시 본다.

공백은 경계가 **아니다** — `blue eyes`처럼 낱말이 여럿인 태그를 찾아야 하기 때문이다. 대신
구간 전체로 후보를 못 찾았을 때를 위해 `trailing_word()`가 마지막 낱말만 떼어 준다 (쉼표 없이
이어 쓴 `1girl, solo masterpi` 같은 경우 — 자동완성 쪽에서 두 번째 시도로 쓴다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .wildcards.catalog import CLOSERS, WildcardToken

#: 토큰을 끊는 문자 — 쉼표 · 줄바꿈 · 캐릭터 구분자.
SEPARATORS = ",\n|"

#: 와일드카드 여는 기호 (`__=`를 `__`보다 먼저 물도록 `=?`).
_OPENER_RE = re.compile(r"##|__=?")

#: 토큰 안의 마지막 낱말 (`trailing_word`).
_TRAILING_WORD_RE = re.compile(r"\S+$")


@dataclass(frozen=True)
class PromptToken:
    """커서 바로 앞에서 입력 중인 토큰 하나."""

    #: 토큰 원문 (와일드카드면 여는 기호를 포함한다).
    text: str
    #: 전체 문자열에서의 시작 위치 — 자동완성이 여기서 커서까지를 갈아 끼운다.
    start: int
    #: 와일드카드를 치는 중이면 (여는 기호, 검색어), 아니면 None.
    wildcard: WildcardToken | None = None


def token_at_cursor(text: str, cursor_pos: int) -> PromptToken:
    """커서 앞 토큰을 돌려준다 (없으면 빈 토큰)."""
    before = text[:cursor_pos]

    # 1) 구분자 뒤 = 이 토큰이 속한 구간
    seg_start = max((before.rfind(sep) for sep in SEPARATORS), default=-1) + 1
    segment = before[seg_start:]

    # 2) 구간 안의 와일드카드를 왼쪽부터 훑는다. 닫힌 것은 건너뛰고, 닫히지 않은 것이
    #    나오면 그게 지금 치고 있는 토큰이다.
    pos = 0
    after_closed = 0  # 마지막으로 닫힌 와일드카드 뒤 — 태그 토큰은 여기서 시작한다
    while True:
        match = _OPENER_RE.search(segment, pos)
        if match is None:
            break
        opener = match.group()
        body_start = match.end()
        close_at = segment.find(CLOSERS[opener], body_start)
        if close_at == -1:
            return PromptToken(
                text=segment[match.start() :],
                start=seg_start + match.start(),
                wildcard=WildcardToken(opener=opener, keyword=segment[body_start:]),
            )
        pos = after_closed = close_at + len(CLOSERS[opener])

    # 3) 와일드카드가 아니면 태그 토큰 — 앞뒤 공백은 토큰에 넣지 않는다
    rest = segment[after_closed:]
    stripped = rest.lstrip()
    return PromptToken(text=stripped, start=seg_start + after_closed + (len(rest) - len(stripped)))


def trailing_word(token: PromptToken) -> PromptToken | None:
    """토큰의 **마지막 낱말**만 떼어 낸 토큰. 낱말이 하나뿐이면 None.

    구간 전체(`1girl, solo masterpi` → `solo masterpi`)로는 후보가 없을 때 한 번 더 찾아볼
    자리다. 시작 위치도 함께 옮겨 주므로, 고른 값은 마지막 낱말만 갈아 끼운다 (앞 낱말은
    그대로 남는다). 와일드카드 토큰에는 쓰지 않는다 — 거기서는 여는 기호가 이미 경계다.
    """
    if token.wildcard is not None:
        return None
    match = _TRAILING_WORD_RE.search(token.text)
    if match is None or match.start() == 0:
        return None  # 공백이 없거나(낱말 하나) 공백으로 끝난다
    return PromptToken(text=match.group(), start=token.start + match.start())


__all__ = ["SEPARATORS", "PromptToken", "token_at_cursor", "trailing_word"]
