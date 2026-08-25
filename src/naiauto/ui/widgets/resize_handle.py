"""드래그로 대상 위젯의 높이를 변경하는 핸들 바.

Prompt_Tabs 위젯 아래에 삽입되어 사용자가 수직 드래그로 편집 영역의 높이를
조절할 수 있게 한다. 높이는 QSettings에 저장되어 세션 간 유지된다.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

MIN_HEIGHT = 120
MAX_HEIGHT = 600
DEFAULT_HEIGHT = 230
SETTINGS_KEY = "ui/prompt_tabs_height"

#: 핸들 바의 고정 높이 (px).
HANDLE_HEIGHT = 6
#: 그립 선 길이 (px).
GRIP_LINE_WIDTH = 30
#: 그립 선 개수.
GRIP_LINE_COUNT = 3
#: 그립 선 간 세로 간격 (px).
GRIP_LINE_SPACING = 2


class ResizeHandle(QWidget):
    """드래그로 대상 위젯의 높이를 변경하는 6px 핸들 바."""

    height_persisted = Signal()

    MIN_HEIGHT = MIN_HEIGHT
    MAX_HEIGHT = MAX_HEIGHT
    DEFAULT_HEIGHT = DEFAULT_HEIGHT
    SETTINGS_KEY = SETTINGS_KEY

    def __init__(
        self,
        target: QWidget,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        settings_key: str = SETTINGS_KEY,
        default_height: int = DEFAULT_HEIGHT,
    ) -> None:
        super().__init__(parent)
        self._target = target
        self._settings = settings
        self._settings_key = settings_key
        self._default_height = default_height

        self._drag_start_y: int | None = None
        self._drag_start_height: int | None = None

        self.setFixedHeight(HANDLE_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    # ── 높이 복원 ────────────────────────────────────────

    def restore_height(self) -> None:
        """QSettings에서 높이를 읽어 대상 위젯에 적용한다.

        저장된 값이 없거나 [120, 600] 범위 밖이면 기본값을 사용한다.
        """
        raw = self._settings.value(self._settings_key)
        height = self._default_height
        if raw is not None:
            try:
                val = int(raw)
                if MIN_HEIGHT <= val <= MAX_HEIGHT:
                    height = val
            except (TypeError, ValueError):
                pass
        self._target.setFixedHeight(height)

    # ── 마우스 이벤트 ────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_y = int(event.globalPosition().y())
            self._drag_start_height = self._target.height()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_start_y is not None and self._drag_start_height is not None:
            delta = int(event.globalPosition().y()) - self._drag_start_y
            new_height = max(MIN_HEIGHT, min(MAX_HEIGHT, self._drag_start_height + delta))
            self._target.setFixedHeight(new_height)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start_y is not None:
            height = self._target.height()
            self._settings.setValue(self._settings_key, height)
            self._drag_start_y = None
            self._drag_start_height = None
            self.height_persisted.emit()
        super().mouseReleaseEvent(event)

    # ── 그립 표시 ────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = self.palette().mid().color()
        pen = QPen(color, 1)
        painter.setPen(pen)

        cx = self.width() / 2
        cy = self.height() / 2

        total_h = (GRIP_LINE_COUNT - 1) * GRIP_LINE_SPACING
        start_y = cy - total_h / 2

        for i in range(GRIP_LINE_COUNT):
            y = int(start_y + i * GRIP_LINE_SPACING)
            x1 = int(cx - GRIP_LINE_WIDTH / 2)
            x2 = int(cx + GRIP_LINE_WIDTH / 2)
            painter.drawLine(x1, y, x2, y)

        painter.end()
