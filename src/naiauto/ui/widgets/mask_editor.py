"""인페인팅 마스크 편집기 — 원본 위에 브러시로 칠해서 마스크를 만든다.

마스크는 원본 해상도의 QImage(Grayscale8)로 관리하고, 확정 시 PNG 바이트로
내보낸다. 흰색(255) = 다시 생성할 영역.
"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager

DEFAULT_BRUSH = 48
MIN_BRUSH = 4
MAX_BRUSH = 300


class MaskCanvas(QWidget):
    """원본을 배경으로 깔고 마스크를 반투명하게 겹쳐 그리는 캔버스."""

    def __init__(self, source: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self.mask = QImage(source.size(), QImage.Format.Format_Grayscale8)
        self.mask.fill(0)
        self.brush_size = DEFAULT_BRUSH
        self.erasing = False
        self._last_point: QPoint | None = None
        self.setMinimumSize(320, 320)

    # ── 좌표 변환 ────────────────────────────────────────

    def _scaled_rect(self) -> tuple[float, float, float]:
        """(offset_x, offset_y, scale) — 원본을 위젯에 맞춰 letterbox 배치."""
        if self._source.width() == 0 or self._source.height() == 0:
            return 0.0, 0.0, 1.0
        scale = min(self.width() / self._source.width(), self.height() / self._source.height())
        offset_x = (self.width() - self._source.width() * scale) / 2
        offset_y = (self.height() - self._source.height() * scale) / 2
        return offset_x, offset_y, scale

    def widget_to_image(self, pos: QPointF) -> QPoint:
        offset_x, offset_y, scale = self._scaled_rect()
        if scale == 0:
            return QPoint(0, 0)
        return QPoint(int((pos.x() - offset_x) / scale), int((pos.y() - offset_y) / scale))

    # ── 그리기 ───────────────────────────────────────────

    def paint_stroke(self, start: QPoint, end: QPoint) -> None:
        """이미지 좌표계에서 start→end 선분을 브러시 굵기로 칠한다."""
        painter = QPainter(self.mask)
        color = QColor(0, 0, 0) if self.erasing else QColor(255, 255, 255)
        pen = QPen(color, self.brush_size)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if start == end:
            painter.drawPoint(start)  # 한 번 클릭 = 점 하나 (drawLine은 아무것도 그리지 않는다)
        else:
            painter.drawLine(start, end)
        painter.end()
        self.update()

    def clear(self) -> None:
        self.mask.fill(0)
        self.update()

    def has_content(self) -> bool:
        """칠해진 픽셀이 하나라도 있는가."""
        # Grayscale8 버퍼를 통째로 훑는다. 축소본 샘플링은 작은 점을 놓칠 수 있다.
        data = self.mask.constBits().tobytes()
        return data.count(0) != len(data)

    # ── Qt 이벤트 ────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 콜백)
        point = self.widget_to_image(event.position())
        self._last_point = point
        self.paint_stroke(point, point)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._last_point is None:
            return
        point = self.widget_to_image(event.position())
        self.paint_stroke(self._last_point, point)
        self._last_point = point

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._last_point = None

    def paintEvent(self, event) -> None:  # noqa: N802
        offset_x, offset_y, scale = self._scaled_rect()
        width = int(self._source.width() * scale)
        height = int(self._source.height() * scale)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        painter.drawPixmap(
            int(offset_x),
            int(offset_y),
            QPixmap.fromImage(self._source).scaled(
                width,
                height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ),
        )
        # 마스크를 반투명 빨강으로 겹쳐 보여준다
        overlay = QImage(self.mask.size(), QImage.Format.Format_ARGB32)
        overlay.fill(Qt.GlobalColor.transparent)
        tint = QPainter(overlay)
        tint.drawImage(0, 0, self.mask)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(overlay.rect(), QColor(255, 60, 60, 130))
        tint.end()
        painter.drawImage(
            int(offset_x),
            int(offset_y),
            overlay.scaled(
                width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
            ),
        )
        painter.end()


class MaskEditorDialog(QDialog):
    """마스크 편집 다이얼로그. 수락 시 mask_bytes()가 PNG를 돌려준다."""

    def __init__(
        self, i18n: I18nManager, image_bytes: bytes, initial_mask: bytes | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        tr = i18n.get_text

        source = QImage()
        source.loadFromData(QByteArray(image_bytes))
        self.canvas = MaskCanvas(source, self)
        if initial_mask:
            existing = QImage()
            if existing.loadFromData(QByteArray(initial_mask)) and not existing.isNull():
                self.canvas.mask = existing.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
                    source.size()
                )

        self.setWindowTitle(tr("mask_editor.title"))
        self.resize(820, 720)

        layout = QVBoxLayout(self)
        hint = QLabel(tr("mask_editor.hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(self.canvas, stretch=1)

        controls = QHBoxLayout()
        controls.addWidget(QLabel(tr("mask_editor.brush")))
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(MIN_BRUSH, MAX_BRUSH)
        self.brush_slider.setValue(DEFAULT_BRUSH)
        self.brush_slider.valueChanged.connect(self._on_brush_changed)
        controls.addWidget(self.brush_slider, stretch=1)
        self.brush_value = QLabel(str(DEFAULT_BRUSH))
        controls.addWidget(self.brush_value)

        self.eraser_check = QCheckBox(tr("mask_editor.eraser"))
        self.eraser_check.toggled.connect(self._on_eraser_toggled)
        controls.addWidget(self.eraser_check)

        self.clear_button = QPushButton(tr("mask_editor.clear"))
        self.clear_button.clicked.connect(self.canvas.clear)
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("mask_editor.done"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("mask_editor.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_brush_changed(self, value: int) -> None:
        self.canvas.brush_size = value
        self.brush_value.setText(str(value))

    def _on_eraser_toggled(self, on: bool) -> None:
        self.canvas.erasing = on

    def has_mask(self) -> bool:
        return self.canvas.has_content()

    def mask_bytes(self) -> bytes | None:
        """칠한 내용이 있으면 원본 해상도 흑백 PNG 바이트, 없으면 None."""
        if not self.has_mask():
            return None
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        self.canvas.mask.save(buffer, "PNG")
        return bytes(buffer.data())


def qimage_to_png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


__all__ = ["DEFAULT_BRUSH", "MaskCanvas", "MaskEditorDialog", "qimage_to_png"]
