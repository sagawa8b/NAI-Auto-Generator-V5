"""프롬프트 잠금 — `<lock>...</lock>` 구간은 설정을 불러와도 유지된다 (Qt-free core).

작가 조합이나 공통 키워드를 잠가 두면, 설정 파일·프리셋을 불러오거나 세팅별 연속 생성이
파일을 갈아 끼울 때 그 구간이 불러온 프롬프트 **앞에** 그대로 붙는다.

    지금 프롬프트   <lock>1girl, solo, artist:aaa, artist:bbb,</lock> 아무거나
    01.json         wedding dress, wedding ring
    불러온 결과     <lock>1girl, solo, artist:aaa, artist:bbb,</lock> wedding dress, wedding ring

규칙:
- 지금 칸에 잠금 구간이 **없으면** 불러온 값을 그대로 쓴다 (기존 동작과 같다).
- 있으면 잠금 구간을 모아 앞에 두고, 불러온 값에서는 그 값 자신의 잠금 구간을 뺀다 —
  잠근 채 저장한 설정 파일을 다시 불러와도 같은 구간이 두 번 붙지 않는다.
- 캐릭터 프롬프트는 슬롯 번호끼리 맞춘다. 불러온 설정에 그 번호의 캐릭터가 없어도 잠긴
  슬롯은 잠금 구간만 남겨 유지한다 — 잠금은 "어느 설정이든 항상 넣는다"는 뜻이다.

표식은 NovelAI로 나가기 전에 지운다 (`strip_lock_markers`, 내용은 남긴다). 꺾쇠는
NovelAI 문법에 없고, `<a|b>` 선택 문법은 `|`가 있을 때만 동작하므로 서로 겹치지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

LOCK_OPEN = "<lock>"
LOCK_CLOSE = "</lock>"

#: 잠금 구간 하나 — 줄바꿈을 넘어도 된다. 닫히지 않은 `<lock>`은 구간으로 보지 않는다.
_BLOCK_RE = re.compile(r"<lock>.*?</lock>", re.IGNORECASE | re.DOTALL)
#: 표식만 (여닫는 쪽 모두). 짝이 안 맞는 표식도 서버로 새지 않게 지운다.
_MARKER_RE = re.compile(r"</?lock>", re.IGNORECASE)

_REPEATED_COMMA_RE = re.compile(r",(?:\s*,)+")
_EDGE_RE = re.compile(r"^[\s,]+|[\s,]+$")

__all__ = [
    "LOCK_CLOSE",
    "LOCK_OPEN",
    "apply_locked",
    "apply_locked_characters",
    "has_lock",
    "locked_part",
    "lock_spans",
    "strip_lock_markers",
    "toggle_lock",
]


def has_lock(text: str) -> bool:
    """닫힌 잠금 구간이 하나라도 있으면 True."""
    return bool(_BLOCK_RE.search(text))


def locked_part(text: str) -> str:
    """잠금 구간들을 표식째 모아 돌려준다 (여럿이면 `, `로 잇는다). 없으면 빈 문자열."""
    return ", ".join(match.group(0) for match in _BLOCK_RE.finditer(text))


def strip_lock_markers(text: str) -> str:
    """표식만 지우고 내용은 남긴다 — 생성 요청에 쓰는 실제 프롬프트."""
    if "lock>" not in text.lower():
        return text
    return _MARKER_RE.sub("", text)


def lock_spans(text: str) -> list[tuple[int, int]]:
    """잠금 구간들의 (시작, 끝) 위치 — 표식 포함. 편집기 강조가 쓴다."""
    return [match.span() for match in _BLOCK_RE.finditer(text)]


def toggle_lock(text: str, start: int, end: int) -> tuple[str, int, int] | None:
    """편집기의 `잠금 / 잠금 해제` — 바뀐 텍스트와 새 선택 범위를 돌려준다.

    - 커서(또는 선택 전체)가 잠금 구간 안이면 그 구간을 풀어 준다 (내용은 남는다).
    - 아니면 선택 영역 안의 표식을 지우거나(풀기), 표식이 없으면 선택 영역을 잠근다.
    - 선택도 없고 잠금 구간 안도 아니면 할 일이 없다 (None).
    """
    start, end = min(start, end), max(start, end)
    for match in _BLOCK_RE.finditer(text):
        if match.start() <= start and end <= match.end():
            inner = text[match.start() + len(LOCK_OPEN) : match.end() - len(LOCK_CLOSE)]
            new_text = text[: match.start()] + inner + text[match.end() :]
            return new_text, match.start(), match.start() + len(inner)
    if start == end:
        return None
    segment = text[start:end]
    if _MARKER_RE.search(segment):
        replaced = _MARKER_RE.sub("", segment)
    else:
        replaced = f"{LOCK_OPEN}{segment}{LOCK_CLOSE}"
    return text[:start] + replaced + text[end:], start, start + len(replaced)


def _without_locked(text: str) -> str:
    """잠금 구간을 내용째 지우고, 남은 빈 쉼표·양끝 쉼표를 정리한다."""
    if not has_lock(text):
        return text
    removed = _BLOCK_RE.sub("", text)
    removed = _REPEATED_COMMA_RE.sub(",", removed)
    return _EDGE_RE.sub("", removed)


def _join(locked: str, rest: str) -> str:
    """잠금 구간 뒤에 불러온 값을 잇는다 — 표식을 지운 결과가 `a, b` 모양이 되게.

    잠금 내용이 `, `로 끝나면 그대로 붙이고, `,`로 끝나면 공백만, 아니면 `, `를 넣는다.
    """
    rest = rest.strip()
    if not rest:
        return locked
    inner = _MARKER_RE.sub("", locked)
    if inner.rstrip().endswith(","):
        separator = "" if inner != inner.rstrip() else " "
    else:
        separator = ", "
    return f"{locked}{separator}{rest}"


def apply_locked(current: str, loaded: str) -> str:
    """불러온 값 `loaded`에 지금 칸 `current`의 잠금 구간을 앞에 붙여 돌려준다.

    지금 칸에 잠금이 없으면 `loaded`를 그대로 돌려준다.
    """
    locked = locked_part(current)
    if not locked:
        return loaded
    return _join(locked, _without_locked(loaded))


def apply_locked_characters(
    current: Sequence[tuple[str, str]],
    loaded: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """캐릭터 슬롯별 (프롬프트, 네거티브)에 잠금을 적용한다.

    결과 길이는 불러온 캐릭터 수와 "잠금이 있는 마지막 슬롯 번호" 중 큰 쪽이다. 불러온
    설정에 없는 자리는 빈 값으로 보고 잠금 구간만 남긴다 (사이에 낀 잠금 없는 자리는 빈
    슬롯이 되며, 빈 캐릭터 프롬프트는 생성 요청에 들어가지 않는다).
    """
    last_locked = -1
    for index, (prompt, uc) in enumerate(current):
        if has_lock(prompt) or has_lock(uc):
            last_locked = index
    size = max(len(loaded), last_locked + 1)
    result: list[tuple[str, str]] = []
    for index in range(size):
        loaded_prompt, loaded_uc = loaded[index] if index < len(loaded) else ("", "")
        if index <= last_locked:
            current_prompt, current_uc = current[index]
            result.append((apply_locked(current_prompt, loaded_prompt), apply_locked(current_uc, loaded_uc)))
        else:
            result.append((loaded_prompt, loaded_uc))
    return result
