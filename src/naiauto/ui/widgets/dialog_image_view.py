"""이미지 정보 대화상자 전용 확대/축소 뷰.

ZoomableImageView를 상속하며, 확대/축소 범위를 [0.1, 5.0]으로 조정하고
더블클릭 시 1.0(패널 맞춤)으로 복원한다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from .zoomable_image_view import ZoomableImageView

_MIN_ZOOM = 0.1
_MAX_ZOOM = 5.0
_ZOOM_STEP = 1.15


class DialogImageView(ZoomableImageView):
    """ImageInfoDialog용 이미지 뷰. 축소(0.1×)까지 허용하고 더블클릭으로 리셋."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 새 이미지를 로드할 때 부모가 _MIN_ZOOM(1.0)으로 초기화하므로 그대로 둔다.

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._source is None:
            # 부모의 QScrollArea.wheelEvent를 호출
            super(ZoomableImageView, self).wheelEvent(event)
            return

        notches = event.angleDelta().y() / 120.0
        if notches == 0:
            return
        old_zoom = self._zoom
        new_zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, old_zoom * (_ZOOM_STEP**notches)))
        if new_zoom == old_zoom:
            event.accept()
            return

        anchor = event.position().toPoint()
        h_bar, v_bar = self.horizontalScrollBar(), self.verticalScrollBar()
        content_x = h_bar.value() + anchor.x()
        content_y = v_bar.value() + anchor.y()
        ratio = new_zoom / old_zoom

        self._zoom = new_zoom
        self._relayout()

        h_bar.setValue(round(content_x * ratio - anchor.x()))
        v_bar.setValue(round(content_y * ratio - anchor.y()))
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """더블클릭 시 확대율을 1.0(패널 맞춤)으로 복원."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._zoom = 1.0
            self._relayout()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    @property
    def zoom(self) -> float:
        """현재 확대율. 1.0 = 뷰포트에 맞춘 크기."""
        return self._zoom


__all__ = ["DialogImageView"]
