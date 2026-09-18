"""프롬프트 `태그 정리` — NovelAI 가중치 문법을 표준 표기로 다듬는 순수 함수 (Qt-free).

NovelAI 공식 문서의 수치 강조 문법은 `1.5::rain, night ::` 처럼 쓴다.
- 여는 쪽은 `숫자::` (예: `1.5::`, `-2::`),
- 닫는 쪽은 숫자 없는 `::` 이고 그 **앞에 공백을 하나** 둔다,
- 새 `숫자::`가 나오면 앞의 블록을 먼저 닫아 강조가 겹치지 않게 한다.

여기서 하는 것은 **결정적이고 안전한 정리**뿐이다 — 사용자가 무엇을 원했는지
추측해야 하는 경우는 만들지 않는다. 문법 오류를 골라 보여 주는 `문법검사`는 이
엔진 위에 얹을 수 있게 남겨 둔다.

정리 규칙 (사용자 확인):
- `1.2::AAA::`          → `1.2::AAA ::`          (닫는 `::` 앞 공백 — 표준 표기)
- `2:AAA`               → `2::AAA ::`            (콜론 하나 → `::`, 끝에서 닫기)
- `2:: BBB ::`          → `2::BBB ::`            (여는 `::` 뒤 군더더기 공백 제거)
- `0.4::AAA, 3::BBB ::` → `0.4::AAA ::, 3::BBB ::` (새 강조 전에 앞 블록을 닫음)
- `3::BBB ::`           → 그대로                 (정수·높은 가중치도 유효)
- 줄바꿈 → `, `, 겹친 쉼표·공백은 하나로.

구현: 문자열을 `숫자::`(여는 표기)·`::`(닫는 표기)·일반 텍스트의 토큰 흐름으로 보고
왼쪽에서 오른쪽으로 훑으며, 강조가 열려 있는지를 상태로 들고 다시 쓴다.
"""

from __future__ import annotations

import re

#: 가중치 표기 토큰. `숫자::`(여는), `숫자:`(오타 — 여는으로 승격), 숫자 없는 `::`(닫는).
#: `\s*::`는 여는 뒤 공백까지 삼켜, 우리가 붙이는 표준 표기로 다시 쓴다.
_MARKER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(::?)|(::)")

#: 겹친 쉼표(사이에 공백만) → 하나.
_DUP_COMMA_RE = re.compile(r"(?:\s*,\s*){2,}")


def clean_prompt(text: str) -> str:
    """프롬프트의 가중치 문법·공백을 표준 표기로 다듬어 돌려준다.

    태그의 의미(작가·개념)는 그대로 두고 강조 문법의 **모양**만 표준에 맞춘다.
    빈 문자열은 그대로 (양끝 공백만 제거) 돌려준다.
    """
    if not text.strip():
        return ""

    # 줄바꿈은 쉼표로 이어 붙인다 — API에는 `\n`이 그대로 나가 미묘하게 결과를 바꾼다.
    # 자동이 아니라 사용자가 누르는 버튼이므로 여기서 명시적으로 정리한다.
    working = re.sub(r"[\r\n]+", ", ", text)

    parts: list[str] = []  # 지금까지 만든 출력 조각들
    open_block = False  # `숫자::`로 연 강조 안에 있는가
    pos = 0

    def close_current() -> None:
        """열린 블록을 표준 표기(` ::`)로 닫는다. 직전 텍스트의 꼬리 공백·쉼표는 정리."""
        nonlocal open_block
        while parts and parts[-1].strip() == "":
            parts.pop()
        if parts:
            parts[-1] = parts[-1].rstrip().rstrip(",").rstrip()
        parts.append(" ::")
        open_block = False

    for m in _MARKER_RE.finditer(working):
        parts.append(working[pos : m.start()])
        pos = m.end()

        if m.group(3) is not None:
            # 숫자 없는 `::` — 닫는 표기.
            if open_block:
                close_current()
            else:
                # 짝 없는 닫는 `::` — 사용자가 남긴 것. 표준 공백만 맞춰 그대로 둔다.
                parts.append(" ::")
        else:
            # `숫자::` 또는 `숫자:` — 여는 표기. 콜론 개수와 무관하게 `::`로 승격한다.
            number = m.group(1)
            if open_block:  # 앞 블록을 먼저 닫아 강조 중첩을 막는다
                close_current()
                parts.append(", ")
            parts.append(f"{number}::")
            open_block = True

    parts.append(working[pos:])
    if open_block:  # 우리가 연 블록이 끝까지 안 닫혔으면 끝에서 닫는다
        close_current()

    return _tidy_spacing("".join(parts))


def _tidy_spacing(text: str) -> str:
    """여는 `::` 뒤 군더더기 공백, 닫는 `::` 앞 공백, 겹친 쉼표·공백을 표준으로."""
    text = re.sub(r"(-?\d+(?:\.\d+)?::)\s+", r"\1", text)  # 여는 `::` 뒤 공백 제거
    text = re.sub(r"\s*::", " ::", text)  # 닫는 `::` 앞은 공백 하나
    text = re.sub(r"(-?\d+(?:\.\d+)?) ::", r"\1::", text)  # 단, 여는 `숫자 ::`는 붙인다
    text = _DUP_COMMA_RE.sub(", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)  # 쉼표 앞 공백 제거
    text = re.sub(r",(?=\S)", ", ", text)  # 쉼표 뒤 공백 하나
    return text.strip().strip(",").strip()


__all__ = ["clean_prompt"]
