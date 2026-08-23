"""스크롤 중 값이 바뀌는 사고를 막는 휠 가드.

콤보/스핀박스/슬라이더/탭바가 스크롤 영역 안에 있으면, 패널을 스크롤하다 커서가
지나가는 것만으로 모델·해상도·스텝 같은 값이 바뀐다. 이 위젯들이 휠을 처리하지
못하게 막고, 대신 바깥 스크롤 영역을 직접 움직여 준다.
(드롭다운을 펼친 상태의 목록 스크롤은 팝업 뷰가 따로 받으므로 영향 없다.)

주의: Qt가 처리 중인 이벤트 객체를 다른 위젯에 다시 보내면(sendEvent) 프로세스가
죽는다. 스크롤바 값을 직접 옮기는 쪽이 안전하고 동작도 결정적이다.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QTabBar,
    QWidget,
)

GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QAbstractSlider, QTabBar)
WHEEL_NOTCH = 120.0  # 휠 한 칸의 angleDelta


def _enclosing_scroll_area(widget: QObject) -> QAbstractScrollArea | None:
    """위젯을 담고 있는 바깥 스크롤 영역 (없으면 None)."""
    parent = widget.parent() if isinstance(widget, QWidget) else None
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parent()
    return None


class WheelGuard(QObject):
    """설치된 위젯에서 휠로 값이 바뀌지 않게 하고, 스크롤은 패널에 넘긴다."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt 콜백)
        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(watched, event)
        self._scroll_enclosing_area(watched, event)
        # accept + True: 위젯도 처리하지 않고 부모로도 전파되지 않는다 (이중 스크롤 방지)
        event.accept()
        return True

    @staticmethod
    def _scroll_enclosing_area(watched: QObject, event: QEvent) -> None:
        area = _enclosing_scroll_area(watched)
        if area is None:
            return
        bar = area.verticalScrollBar()
        pixels = event.pixelDelta().y()  # 트랙패드 등 픽셀 단위 스크롤
        if pixels:
            bar.setValue(bar.value() - pixels)
            return
        notches = event.angleDelta().y() / WHEEL_NOTCH
        lines = notches * QApplication.wheelScrollLines()
        bar.setValue(bar.value() - round(lines * bar.singleStep()))


def guard_wheel(root: QWidget, guard: WheelGuard | None = None) -> WheelGuard:
    """root 아래의 값 위젯 전부에 휠 가드를 건다.

    반환한 가드는 호출자가 붙들고 있어야 한다 (GC되면 필터가 풀린다).
    나중에 만들어지는 위젯(캐릭터 슬롯 등)은 만들 때 같은 가드로 다시 호출한다.
    """
    guard = guard or WheelGuard(root)
    for widget_type in GUARDED_TYPES:
        for widget in root.findChildren(widget_type):
            widget.installEventFilter(guard)
    if isinstance(root, GUARDED_TYPES):
        root.installEventFilter(guard)
    return guard


__all__ = ["GUARDED_TYPES", "WheelGuard", "guard_wheel"]
