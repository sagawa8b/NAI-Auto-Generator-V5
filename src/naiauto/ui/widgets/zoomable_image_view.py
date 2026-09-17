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
from PySide6.QtWidgets import QScrollArea, QWidget

from .hidpi_image import HiDpiImageLabel, scaled_for_screen

_MIN_ZOOM = 1.0
_MAX_ZOOM = 8.0
_ZOOM_STEP = 1.15  # 휠 한 칸(각도 120)당 배율 — wheel_guard.py의 WHEEL_NOTCH와 같은 단위


class ZoomableImageView(QScrollArea):
    """QLabel + QScrollArea 조합을 대체하는 확대/축소 가능 미리보기.

    main_window.py의 기존 preview_label 호출부(setPixmap/pixmap/setText/setToolTip)와
    호환되는 이름의 메서드를 제공해, 호출부는 바꾸지 않아도 되게 한다.
    """

    def __init__(self, parent: QWidget | None = None, *, min_size: int = 320) -> None:
        super().__init__(parent)
        self._label = HiDpiImageLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(min_size, min_size)
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

    def has_image(self) -> bool:
        """그림을 들고 있으면 True (텍스트만 보이는 상태와 구분)."""
        return self._source is not None

    def setText(self, text: str) -> None:  # noqa: N802
        self._source = None
        self._label.setText(text)
        self._label.resize(self._label.sizeHint())

    def text(self) -> str:
        """현재 대체 텍스트 (그림이 없을 때 안내 문구). 그림이 있으면 빈 문자열."""
        return self._label.text()

    def setToolTip(self, text: str) -> None:  # noqa: N802
        self._label.setToolTip(text)

    # ── 확대/축소 ────────────────────────────────────────────────────

    @property
    def zoom(self) -> float:
        """현재 확대율. 1.0 = 뷰포트에 맞춘 크기."""
        return self._zoom

    def can_zoom_in(self) -> bool:
        """더 확대할 여지가 있으면 True (버튼 활성화 판단용)."""
        return self._source is not None and self._zoom < _MAX_ZOOM

    def can_zoom_out(self) -> bool:
        """맞춘 크기보다 더 커져 있어 축소할 여지가 있으면 True."""
        return self._source is not None and self._zoom > _MIN_ZOOM

    def zoom_in(self) -> None:
        """뷰포트 중심을 기준으로 한 칸 확대한다 (버튼용)."""
        self._zoom_at(self._viewport_center(), _ZOOM_STEP)

    def zoom_out(self) -> None:
        """뷰포트 중심을 기준으로 한 칸 축소한다 (버튼용)."""
        self._zoom_at(self._viewport_center(), 1.0 / _ZOOM_STEP)

    def fit(self) -> None:
        """맞춘 크기(확대율 1.0)로 되돌린다."""
        if self._source is None or self._zoom == _MIN_ZOOM:
            return
        self._zoom = _MIN_ZOOM
        self._relayout()

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
        if self._zoom_at(event.position().toPoint(), _ZOOM_STEP**notches):
            event.accept()

    def _viewport_center(self):
        rect = self.viewport().rect()
        return rect.center()

    def _zoom_at(self, anchor, factor: float) -> bool:
        """`anchor`(뷰포트 좌표) 아래의 이미지 지점이 제자리에 남도록 배율을 곱한다.

        실제로 배율이 바뀌었으면 True.
        """
        if self._source is None:
            return False
        old_zoom = self._zoom
        new_zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, old_zoom * factor))
        if new_zoom == old_zoom:
            return False

        h_bar, v_bar = self.horizontalScrollBar(), self.verticalScrollBar()
        content_x = h_bar.value() + anchor.x()
        content_y = v_bar.value() + anchor.y()
        ratio = new_zoom / old_zoom

        self._zoom = new_zoom
        self._relayout()

        h_bar.setValue(round(content_x * ratio - anchor.x()))
        v_bar.setValue(round(content_y * ratio - anchor.y()))
        return True

    def _relayout(self) -> None:
        if self._source is None:
            return
        fit = self._source.size().scaled(self.viewport().size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QSize(round(fit.width() * self._zoom), round(fit.height() * self._zoom))
        if target.width() <= 0 or target.height() <= 0:
            return

        # 논리 픽셀이 아니라 화면이 실제로 찍을 픽셀 수만큼 축소한다 — hidpi_image 참조
        scaled = scaled_for_screen(self._source, target, self.devicePixelRatioF())
        self._label.set_image(scaled)
        # 라벨은 논리 크기로 — 픽스맵이 그 배율만큼의 실제 픽셀을 담고 있다
        self._label.resize(scaled.deviceIndependentSize().toSize())


__all__ = ["ZoomableImageView"]
