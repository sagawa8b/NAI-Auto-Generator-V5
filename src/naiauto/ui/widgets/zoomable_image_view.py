"""마우스 스크롤로 확대/축소하는 이미지 미리보기.

기존에는 QLabel을 QScrollArea(setWidgetResizable=True)에 얹어, 뷰포트에 맞춰
축소된 이미지만 보여주고 그 이상 확대할 방법이 없었다. 여기서는 확대율을 직접
들고 있다가 휠을 굴리면 그 배율로 원본을 다시 스케일링해 라벨에 앉힌다 —
뷰포트보다 커지면 QScrollArea가 자동으로 스크롤바를 보여준다.

확대율 1.0 = 뷰포트에 맞춘 크기(이전과 같은 기본 동작). 그 아래로는 내려가지
않는다 — 이미 다 보이는 이미지를 더 축소해 봐야 여백만 늘어난다.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

_MIN_ZOOM = 1.0
_MAX_ZOOM = 8.0
_ZOOM_STEP = 1.15  # 휠 한 칸(각도 120)당 배율 — wheel_guard.py의 WHEEL_NOTCH와 같은 단위


class ZoomableImageView(QScrollArea):
    """QLabel + QScrollArea 조합을 대체하는 확대/축소 가능 미리보기.

    main_window.py의 기존 preview_label 호출부(setPixmap/pixmap/setText/setToolTip)와
    호환되는 이름의 메서드를 제공해, 호출부는 바꾸지 않아도 되게 한다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(320, 320)
        self.setWidget(self._label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._source: QPixmap | None = None
        self._zoom = _MIN_ZOOM

    # ── main_window.py가 기대하는 QLabel 호환 API ──────────────────────

    def setPixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 (Qt 관례)
        self._source = pixmap if not pixmap.isNull() else None
        self._zoom = _MIN_ZOOM  # 새 이미지는 항상 맞춘 크기에서 시작한다
        self._relayout()

    def pixmap(self) -> QPixmap:  # noqa: N802
        return self._label.pixmap()

    def setText(self, text: str) -> None:  # noqa: N802
        self._source = None
        self._label.setPixmap(QPixmap())
        self._label.setText(text)
        self._label.resize(self._label.sizeHint())

    def setToolTip(self, text: str) -> None:  # noqa: N802
        self._label.setToolTip(text)

    # ── 확대/축소 ────────────────────────────────────────────────────

    @property
    def zoom(self) -> float:
        """현재 확대율. 1.0 = 뷰포트에 맞춘 크기."""
        return self._zoom

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._source is not None:
            self._relayout()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._source is None:
            super().wheelEvent(event)
            return

        notches = event.angleDelta().y() / 120.0
        if notches == 0:
            return
        old_zoom = self._zoom
        new_zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, old_zoom * (_ZOOM_STEP**notches)))
        if new_zoom == old_zoom:
            event.accept()
            return

        # 커서 아래의 이미지 지점이 확대 후에도 같은 화면 위치에 남도록 스크롤을 보정한다.
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

    def _relayout(self) -> None:
        if self._source is None:
            return
        fit = self._source.size().scaled(self.viewport().size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QSize(round(fit.width() * self._zoom), round(fit.height() * self._zoom))
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._source.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())


__all__ = ["ZoomableImageView"]
