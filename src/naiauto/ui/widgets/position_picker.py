"""캐릭터 위치 지정 캔버스 — 생성 해상도 비율에 맞춰 자유 배치.

NovelAI 웹 UI와 동일하게 캔버스를 실제 생성 비율(letterbox)로 그리고, 캐릭터
마커를 끌어서 아무 위치나 지정한다. 5×5 격자선은 구 그리드 좌표(A1~E5)를
눈으로 가늠하기 위한 안내선일 뿐, 값은 격자에 붙지 않는다.

좌표는 payload의 `center` 그대로인 0.0~1.0 정규화 값이다 (좌상단 원점).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

GRID_DIVISIONS = 5  # 안내선 개수 = NAI 5×5 그리드
MARKER_RADIUS = 13
PICK_RADIUS = 26  # 이 거리 안을 누르면 해당 마커를 잡는다
MIN_COORD = 0.01
MAX_COORD = 0.99
CANVAS_HEIGHT = 215


def clamp_coord(value: float) -> float:
    """0.01~0.99로 자르고 소수점 2자리로 정리 (payload가 읽기 쉬운 값이 된다)."""
    return round(min(MAX_COORD, max(MIN_COORD, value)), 2)


class PositionPicker(QWidget):
    """번호가 붙은 마커를 끌어 캐릭터 위치를 지정하는 캔버스."""

    moved = Signal(int, float, float)  # slot_index(0부터), center_x, center_y

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[str, float, float]] = []
        self._aspect = (1.0, 1.0)
        self._dragging: int | None = None
        self.setFixedHeight(CANVAS_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ── 상태 ─────────────────────────────────────────────

    def set_aspect(self, width: int, height: int) -> None:
        """캔버스 비율을 생성 해상도에 맞춘다."""
        if width > 0 and height > 0:
            self._aspect = (float(width), float(height))
            self.update()

    def set_points(self, points: list[tuple[str, float, float]]) -> None:
        """(라벨, x, y) 목록. 순서가 곧 슬롯 인덱스다."""
        self._points = list(points)
        self.update()

    @property
    def points(self) -> list[tuple[str, float, float]]:
        return list(self._points)

    # ── 좌표 변환 ────────────────────────────────────────

    def canvas_rect(self) -> QRectF:
        """위젯 안에서 생성 비율을 유지하는 캔버스 영역."""
        aspect_w, aspect_h = self._aspect
        margin = MARKER_RADIUS + 2  # 마커가 잘리지 않도록 여백
        avail_w = max(1.0, self.width() - 2 * margin)
        avail_h = max(1.0, self.height() - 2 * margin)
        scale = min(avail_w / aspect_w, avail_h / aspect_h)
        width = aspect_w * scale
        height = aspect_h * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def to_widget(self, x: float, y: float) -> QPointF:
        rect = self.canvas_rect()
        return QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())

    def to_coords(self, pos: QPointF) -> tuple[float, float]:
        rect = self.canvas_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return 0.5, 0.5
        return (
            clamp_coord((pos.x() - rect.left()) / rect.width()),
            clamp_coord((pos.y() - rect.top()) / rect.height()),
        )

    def _nearest_point(self, pos: QPointF) -> int | None:
        best: tuple[float, int] | None = None
        for index, (_, x, y) in enumerate(self._points):
            delta = self.to_widget(x, y) - pos
            distance = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
            if distance <= PICK_RADIUS and (best is None or distance < best[0]):
                best = (distance, index)
        return None if best is None else best[1]

    # ── 마우스 ───────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 콜백)
        if not self._points:
            return
        index = self._nearest_point(event.position())
        if index is None:
            return  # 빈 곳 클릭은 무시 — 실수로 마커가 날아가지 않게
        self._dragging = index
        self._move_to(index, event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging is not None:
            self._move_to(self._dragging, event.position())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = None

    def _move_to(self, index: int, pos: QPointF) -> None:
        x, y = self.to_coords(pos)
        label = self._points[index][0]
        self._points[index] = (label, x, y)
        self.update()
        self.moved.emit(index, x, y)

    # ── 그리기 ───────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        rect = self.canvas_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(rect, QColor(48, 48, 54) if self._is_dark() else QColor(238, 238, 242))
        painter.setPen(QPen(QColor(120, 120, 130, 120), 1, Qt.PenStyle.DashLine))
        for i in range(1, GRID_DIVISIONS):
            fraction = i / GRID_DIVISIONS
            painter.drawLine(
                QPointF(rect.left() + rect.width() * fraction, rect.top()),
                QPointF(rect.left() + rect.width() * fraction, rect.bottom()),
            )
            painter.drawLine(
                QPointF(rect.left(), rect.top() + rect.height() * fraction),
                QPointF(rect.right(), rect.top() + rect.height() * fraction),
            )
        painter.setPen(QPen(QColor(140, 140, 150), 1))
        painter.drawRect(rect)

        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        for index, (label, x, y) in enumerate(self._points):
            center = self.to_widget(x, y)
            painter.setBrush(QColor(78, 140, 220) if index != self._dragging else QColor(240, 160, 60))
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawEllipse(center, MARKER_RADIUS, MARKER_RADIUS)
            painter.drawText(
                QRectF(
                    center.x() - MARKER_RADIUS,
                    center.y() - MARKER_RADIUS,
                    MARKER_RADIUS * 2,
                    MARKER_RADIUS * 2,
                ),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        painter.end()

    def _is_dark(self) -> bool:
        return self.palette().window().color().lightness() < 128


__all__ = ["CANVAS_HEIGHT", "GRID_DIVISIONS", "PositionPicker", "clamp_coord"]
