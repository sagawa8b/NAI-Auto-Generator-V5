"""아레나 창의 버튼 등급 — 주 동작 · 위험 동작 · 나머지.

지금까지 아레나의 버튼은 전부 같은 크기, 같은 톤이었다. 월드컵 화면 한 장에 버튼이
열일곱 개인데 무엇이 주 동작인지 알 수 없었고, **크레딧을 쓰는 유일한 버튼**인
`그림 생성`이 공짜인 `랜덤 조합 만들기`와 똑같이 생겼으며, 되돌릴 수 없는
`명단 비우기`가 `복사` 옆에 나란히 놓여 있었다.

세 등급만 둔다.

- **주 동작** (`mark_primary`) — 화면당 **하나**. 강조색으로 칠한다.
- **위험 동작** (`mark_danger`) — 되돌릴 수 없는 것. 붉은 글씨와 테두리만 준다
  (칠하지 않는다 — 위험한 것이 화면에서 제일 눈에 띄면 안 된다).
- 나머지는 손대지 않는다. Qt 기본 모양 그대로다.

색은 **팔레트에서 가져온다.** 주 동작은 `palette(highlight)`라 사용자 테마를 그대로
따르고, 붉은색만 팔레트에 대응하는 역할이 없어 창 배경의 밝기를 보고 둘 중 하나를
고른다 — 어두운 테마에서 진한 빨강은 읽히지 않는다.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QAbstractButton

#: `objectName` — QSS 선택자가 이 이름으로 위젯을 찾는다.
ROLE_PRIMARY = "arenaPrimary"
ROLE_DANGER = "arenaDanger"
#: 그림을 끌어다 놓는 과녁 — 회색 안내문 한 줄 대신 눈에 보이는 자리.
ROLE_DROP_ZONE = "arenaDropZone"

#: 위험 동작의 글자색. 밝은 테마용 / 어두운 테마용.
_DANGER_LIGHT = "#a83a2e"
_DANGER_DARK = "#e08579"

#: 상위 티어(S·A)의 글자색.
_GOOD_LIGHT = "#2c7358"
_GOOD_DARK = "#67bd97"

#: 이보다 어두운 창 배경이면 어두운 테마로 본다 (0~255).
_DARK_THRESHOLD = 128

#: 색을 주는 티어. 나머지(B·C)는 기본 글자색 그대로 둔다 — 전부 칠하면 아무것도
#: 눈에 띄지 않는다.
_TOP_TIERS = ("S", "A")
_LOW_TIERS = ("D", "F")

#: 분포 막대의 투명도 범위 (진한 쪽 → 옅은 쪽).
_RAMP_TOP_ALPHA = 235
_RAMP_LOW_ALPHA = 60


def mark_primary(*buttons: QAbstractButton) -> None:
    """이 화면의 주 동작으로 표시한다. 화면당 하나만 쓴다."""
    for button in buttons:
        button.setObjectName(ROLE_PRIMARY)


def mark_danger(*buttons: QAbstractButton) -> None:
    """되돌릴 수 없는 동작으로 표시한다 (삭제·초기화·비우기)."""
    for button in buttons:
        button.setObjectName(ROLE_DANGER)


def is_dark(palette: QPalette) -> bool:
    """창 배경이 어두운 테마인지."""
    return palette.color(QPalette.ColorRole.Window).lightness() < _DARK_THRESHOLD


def danger_color(palette: QPalette) -> str:
    """테마에 맞는 위험색 (다른 위젯도 글자색으로 쓸 수 있게 공개한다)."""
    return _DANGER_DARK if is_dark(palette) else _DANGER_LIGHT


def ramp(palette: QPalette, steps: int) -> list[str]:
    """강조색을 `steps`단으로 흐려 놓은 rgba 목록 — 위가 진하고 아래가 옅다.

    티어·세대 분포 막대가 쓴다. 색을 새로 지어내지 않고 **팔레트의 강조색 하나에서
    투명도만 깎아** 만들므로, 사용자 테마가 무엇이든 한 계통으로 읽힌다.
    """
    if steps <= 0:
        return []
    colour = palette.color(QPalette.ColorRole.Highlight)
    red, green, blue = colour.red(), colour.green(), colour.blue()
    span = _RAMP_TOP_ALPHA - _RAMP_LOW_ALPHA
    return [
        f"rgba({red}, {green}, {blue}, {_RAMP_TOP_ALPHA - round(span * i / max(1, steps - 1))})"
        for i in range(steps)
    ]


def tier_color(palette: QPalette, tier: str) -> str | None:
    """티어 글자색. 색을 주지 않는 티어(B·C)에는 `None`.

    한 줄짜리 문자열이던 카드 머리글에서 티어만 눈에 들어오게 한다. 위·아래만
    칠하고 가운데는 그대로 둔다 — 전부 색이 있으면 결국 아무것도 눈에 띄지 않는다.
    """
    if tier in _TOP_TIERS:
        return _GOOD_DARK if is_dark(palette) else _GOOD_LIGHT
    if tier in _LOW_TIERS:
        return palette.color(QPalette.ColorRole.Mid).name()
    return None


def arena_stylesheet(palette: QPalette) -> str:
    """아레나 창 전체에 붙일 QSS.

    선택자를 `objectName`으로 좁혀, 표시하지 않은 버튼은 Qt 기본 모양 그대로 둔다.
    """
    danger = danger_color(palette)
    return f"""
QPushButton#{ROLE_PRIMARY} {{
    background-color: palette(highlight);
    color: palette(highlighted-text);
    border: 1px solid palette(highlight);
    border-radius: 3px;
    padding: 5px 14px;
    font-weight: bold;
}}
QPushButton#{ROLE_PRIMARY}:hover {{
    background-color: palette(highlight);
    border-color: palette(text);
}}
QPushButton#{ROLE_PRIMARY}:disabled {{
    background-color: palette(button);
    border-color: palette(mid);
    color: palette(mid);
}}
QPushButton#{ROLE_DANGER} {{
    color: {danger};
    border: 1px solid {danger};
    border-radius: 3px;
    padding: 5px 12px;
}}
QPushButton#{ROLE_DANGER}:disabled {{
    color: palette(mid);
    border-color: palette(mid);
}}
QLabel#{ROLE_DROP_ZONE} {{
    border: 2px dashed palette(mid);
    border-radius: 6px;
    color: palette(mid);
    padding: 12px;
}}
QLabel#{ROLE_DROP_ZONE}[dragging="true"] {{
    border-color: palette(highlight);
    color: palette(highlight);
}}
"""


__all__ = [
    "ROLE_DANGER",
    "ROLE_DROP_ZONE",
    "ROLE_PRIMARY",
    "arena_stylesheet",
    "danger_color",
    "is_dark",
    "mark_danger",
    "mark_primary",
    "ramp",
    "tier_color",
]
