"""프롬프트가 매번 다르게 전개되는 문법을 담고 있는지 판정한다 (Qt-free).

"동일 조건 재생성" 감지가 쓴다 — 요청 값이 완전히 같아도 아래 문법이 들어 있으면
생성마다 결과가 달라지므로 중복으로 보면 안 된다.

    __name__ / __=name__   와일드카드 (core/wildcards/applier.py)
    <a|b|c>                꺾쇠 랜덤 선택 (core/prompt_choices.py)
    {artist:그룹}          아티스트 조합 (core/artist_combos.py)
    ||a|b||                NovelAI 공식 Prompt Randomizer — 서버가 매번 고른다
"""

from __future__ import annotations

import re

#: `<a|b|c>` — 꺾쇠 안에 `|`가 있어야 선택 문법이다 (prompt_choices와 같은 판정).
_BRACKET_CHOICE_RE = re.compile(r"<[^<>]*\|[^<>]*>")
#: `{artist:그룹}` / `{artist_loop:그룹}` (artist_combos와 같은 형태).
_ARTIST_RE = re.compile(r"\{artist(?:_loop)?:[^}]+\}")

__all__ = ["has_dynamic_syntax"]


def has_dynamic_syntax(text: str) -> bool:
    """이 텍스트가 생성 때마다 다르게 전개될 수 있으면 True."""
    if text.count("__") >= 2:  # 와일드카드는 `__`로 열고 닫는다
        return True
    if "||" in text:  # NovelAI Prompt Randomizer
        return True
    return bool(_BRACKET_CHOICE_RE.search(text) or _ARTIST_RE.search(text))
