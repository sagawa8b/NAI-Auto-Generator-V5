"""마우스 스크롤로 확대/축소하는 이미지 미리보기.

기존에는 QLabel을 QScrollArea(setWidgetResizable=True)에 얹어, 뷰포트에 맞춰
축소된 이미지만 보여주고 그 이상 확대할 방법이 없었다. 여기서는 확대율을 직접
들고 있다가 휠을 굴리면 그 배율로 원본을 다시 스케일링해 라벨에 앉힌다 —
뷰포트보다 커지면 QScrollArea가 자동으로 스크롤바를 보여준다.

확대율 1.0 = 뷰포트에 맞춘 크기(이전과 같은 기본 동작). 그 아래로는 내려가지
않는다 — 이미 다 보이는 이미지를 더 축소해 봐야 여백만 늘어난다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

_MIN_ZOOM = 1.0
_MAX_ZOOM = 8.0
_ZOOM_STEP = 1.15  # 휠 한 칸(각도 120)당 배율 — wheel_guard.py의 WHEEL_NOTCH와 같은 단위


class _Canvas(QLabel):
    """이미지를 직접 그리는 라벨.

    QLabel.setPixmap()에 화면 배율만큼 큰 픽스맵을 넘기면 Qt가 논리 크기로 다시
    줄여 버려 그 픽셀을 잃는다 (Qt 6.11에서 확인 — 1픽셀 체커보드가 균일한 회색이
    된다). QPainter.drawPixmap()은 배율을 그대로 살려 그리므로, 픽스맵은 우리가 들고
    있다가 paintEvent에서 직접 그린다. 글자(이미지 없을 때 안내문)는 QLabel에 맡긴다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._image: QPixmap | None = None

    @property
    def image(self) -> QPixmap | None:
        return self._image

    def set_image(self, pixmap: QPixmap | None) -> None:
        self._image = pixmap
        if pixmap is not None:
            super().setText("")
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 관례)
        self._image = None
        super().setText(text)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._image is None:
            super().paintEvent(event)
            return
        # 논리 크기 — 픽스맵이 그보다 배율만큼 많은 픽셀을 담고 있다
        size = self._image.deviceIndependentSize()
        painter = QPainter(self)
        painter.drawPixmap(
            QPointF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2),
            self._image,
        )
        painter.end()


class ZoomableImageView(QScrollArea):
    """QLabel + QScrollArea 조합을 대체하는 확대/축소 가능 미리보기.

    main_window.py의 기존 preview_label 호출부(setPixmap/pixmap/setText/setToolTip)와
    호환되는 이름의 메서드를 제공해, 호출부는 바꾸지 않아도 되게 한다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = _Canvas()
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
        """현재 표시 중인 픽스맵 (화면 배율만큼의 실제 픽셀 + devicePixelRatio 태그)."""
        return self._label.image or QPixmap()

    def setText(self, text: str) -> None:  # noqa: N802
        self._source = None
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

        # 논리 픽셀이 아니라 화면이 실제로 찍을 픽셀 수만큼 축소한다. Qt6은 고DPI
        # 스케일링을 끌 수 없어서(V4의 Qt5는 꺼져 있었다) 논리 크기로 넘기면 그릴 때
        # 배율만큼 다시 확대돼 흐려진다 — 원본에 그 픽셀이 남아 있는데도 버리는 셈이다.
        ratio = self.devicePixelRatioF()
        physical = QSize(round(target.width() * ratio), round(target.height() * ratio))
        scaled = self._source.scaled(
            physical, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        scaled.setDevicePixelRatio(ratio)
        self._label.set_image(scaled)
        # 라벨은 논리 크기로 — 픽스맵이 그 배율만큼의 실제 픽셀을 담고 있다
        self._label.resize(scaled.deviceIndependentSize().toSize())


__all__ = ["ZoomableImageView"]
