"""결과 이미지 위에 방금 생성한 프롬프트를 겹쳐 보여 주는 오버레이 (V4 이식).

V4의 `프롬프트 결과 표시` 체크박스가 하던 일이다. 결과 이미지 아래쪽 40%에 반투명
검정 판을 얹고 그 위에 프롬프트·캐릭터·생성 설정을 적는다. 레이아웃에 넣지 않고
호스트 위젯의 자식으로 두어 절대 위치로 배치하므로, 켜고 끈다고 이미지 크기가
달라지지 않는다.

내용 조합은 `core/result_summary.py`가 맡는다 (Qt 없이 검증할 수 있게).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QTextBrowser, QWidget

#: 호스트 높이에서 오버레이가 차지하는 비율과, 그 아래로는 내려가지 않는 하한(px).
HEIGHT_RATIO = 0.40
MIN_HEIGHT = 120

_STYLE = """
QTextBrowser {
    background-color: rgba(0, 0, 0, 170);
    color: #ffffff;
    border: none;
    font-size: 10pt;
    padding: 8px;
}
"""


class PromptOverlay(QTextBrowser):
    """결과 이미지 하단에 겹쳐 뜨는 읽기 전용 텍스트 판."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setStyleSheet(_STYLE)
        # 이미지를 가리는 판이라 포커스까지 가져가면 Tab 순서가 이상해진다.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVisible(False)
        host.installEventFilter(self)

    # ── 표시 ─────────────────────────────────────────────

    def set_active(self, active: bool) -> None:
        """보기 메뉴의 체크 상태를 그대로 받는다."""
        self.setVisible(bool(active))
        if active:
            self.reposition()
            self.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt 콜백)
        if watched is self._host and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.reposition()
        return super().eventFilter(watched, event)

    def reposition(self) -> None:
        """호스트의 보이는 영역 아래쪽에 붙인다 (스크롤 영역이면 스크롤바를 피한다)."""
        area = self._host.viewport() if isinstance(self._host, QAbstractScrollArea) else self._host
        rect = area.geometry() if area is not self._host else self._host.rect()
        height = min(rect.height(), max(MIN_HEIGHT, int(rect.height() * HEIGHT_RATIO)))
        self.setGeometry(rect.x(), rect.y() + rect.height() - height, rect.width(), height)


__all__ = ["HEIGHT_RATIO", "MIN_HEIGHT", "PromptOverlay"]
