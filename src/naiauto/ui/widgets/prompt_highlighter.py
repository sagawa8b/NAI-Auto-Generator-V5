"""프롬프트 구문 강조 — 가중치(`1.5::...::`)와 짝 안 맞는 괄호 (V4.5에서 이식).

V4.5는 `QTextEdit.ExtraSelection`으로 칠했지만 V5의 입력창은 `QPlainTextEdit`이라
`QSyntaxHighlighter`가 자연스럽다. 규칙은 같다:

    1.5::big eyes::   가중치 > 1 → 파랑 + 굵게
    0.5::feet::       가중치 < 1 → 회색
    ::artist:name::   숫자 없는 강조 → 파랑
    (               짝이 없는 괄호 → 빨강

색은 팔레트와 무관한 고정색이다 (V4.5와 같은 값). 어두운 테마에서도 읽히는 채도라
테마별 분기를 두지 않는다.
"""

from __future__ import annotations

import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

#: 숫자 가중치 + 강조 본문: "1.5::text::" / "-2::text" (단일 콜론은 태그의 일부)
_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)::((?:[^:]|:(?!:))*)(?:::)?")

#: 숫자 없는 강조: "::text::"
_PREFIX_RE = re.compile(r"::((?:[^:]|:(?!:))*)(?:::)?")

COLOR_HIGH = QColor(100, 149, 237)  # 강조(>1.0)
COLOR_LOW = QColor(169, 169, 169)  # 약화(<1.0)
COLOR_MARKER = QColor(128, 0, 128)  # 가중치 숫자와 "::"
COLOR_UNMATCHED = QColor(255, 0, 0)  # 짝 없는 괄호

_BRACKET_PAIRS = {"(": ")", "{": "}", "[": "]", "<": ">"}
_CLOSERS = {close: open_ for open_, close in _BRACKET_PAIRS.items()}


def unmatched_bracket_positions(text: str) -> list[int]:
    """짝이 맞지 않는 괄호의 위치 목록 (V4.5 `findBracketHighlights`와 같은 규칙)."""
    stack: list[tuple[str, int]] = []
    unmatched: list[int] = []
    for i, char in enumerate(text):
        if char in _BRACKET_PAIRS:
            stack.append((char, i))
        elif char in _CLOSERS:
            if stack and _BRACKET_PAIRS[stack[-1][0]] == char:
                stack.pop()
            else:
                unmatched.append(i)
    unmatched.extend(position for _char, position in stack)
    return sorted(unmatched)


def _format(color: QColor, *, bold: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(color)
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    return fmt


class PromptHighlighter(QSyntaxHighlighter):
    """프롬프트 입력창 하나에 붙는 강조기. 문서 수명과 함께 산다."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._marker_format = _format(COLOR_MARKER, bold=True)
        self._low_format = _format(COLOR_LOW)
        self._unmatched_format = _format(COLOR_UNMATCHED, bold=True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt 콜백 이름)
        covered: set[int] = set()

        for match in _NUMBER_RE.finditer(text):
            covered.update(range(match.start(), match.end()))
            try:
                weight = float(match.group(1))
            except ValueError:  # 정규식상 도달할 수 없지만 강조가 입력을 막으면 안 된다
                continue
            body_start, body = match.start(2), match.group(2)
            if weight != 1.0:
                self.setFormat(body_start, len(body), self._emphasis_format(weight))
            self.setFormat(match.start(1), body_start - match.start(1), self._marker_format)

        for match in _PREFIX_RE.finditer(text):
            if match.start() in covered or not match.group(1):
                continue
            self.setFormat(match.start(1), len(match.group(1)), self._emphasis_format(1.5))
            self.setFormat(match.start(), 2, self._marker_format)  # "::" 마커만

        for position in unmatched_bracket_positions(text):
            self.setFormat(position, 1, self._unmatched_format)

    def _emphasis_format(self, weight: float) -> QTextCharFormat:
        """가중치가 클수록 굵게 (V4.5와 같은 계산), 1.0 미만은 회색."""
        if weight < 1.0:
            return self._low_format
        fmt = _format(COLOR_HIGH)
        steps = QFont.Weight.Normal.value + int((weight - 1.0) * 3) * 100
        fmt.setFontWeight(QFont.Weight(min(QFont.Weight.Black.value, max(QFont.Weight.Normal.value, steps))))
        return fmt


__all__ = [
    "COLOR_HIGH",
    "COLOR_LOW",
    "COLOR_MARKER",
    "COLOR_UNMATCHED",
    "PromptHighlighter",
    "unmatched_bracket_positions",
]
