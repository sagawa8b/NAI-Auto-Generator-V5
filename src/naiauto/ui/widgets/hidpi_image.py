"""화면 배율(고DPI)을 살려 이미지를 보여주기 위한 공용 도구.

Qt6은 고DPI 스케일링을 끌 수 없다 (V4의 Qt5는 기본으로 꺼져 있어 1 논리 픽셀이
곧 1 화면 픽셀이었다). 그래서 이미지를 **논리 픽셀** 크기로 줄여 넘기면, 화면에
그릴 때 배율만큼 다시 확대되어 흐려진다 — 원본에 그 픽셀이 남아 있는데도 버리고
늘리는 셈이다. 배율 150%에서 750px 자리에 500px짜리를 넣는 격이다.

여기서는 두 가지를 제공한다.

- `scaled_for_screen` / `scaled_to_height_for_screen` — 화면이 실제로 찍을 픽셀
  수만큼 줄이고 그 배율을 픽스맵에 태그한다. QPainter로 직접 그리는 쪽
  (예: 갤러리 델리게이트)은 이것만 있으면 된다.
- `HiDpiImageLabel` — QLabel 대용. **QLabel.setPixmap()은 배율이 태그된 픽스맵을
  논리 크기로 다시 줄여 버려 그 픽셀을 잃는다** (Qt 6.11에서 확인: 1픽셀 체커보드가
  균일한 회색이 된다). QPainter.drawPixmap()은 배율을 살려 그리므로, 픽스맵을 직접
  들고 paintEvent에서 그린다. 글자는 그대로 QLabel에 맡긴다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QLabel


def scaled_for_screen(source: QPixmap, box: QSize, ratio: float) -> QPixmap:
    """`box`(논리 픽셀) 안에 맞추되, 화면이 실제로 찍을 픽셀은 남긴다."""
    physical = QSize(round(box.width() * ratio), round(box.height() * ratio))
    scaled = source.scaled(
        physical, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    scaled.setDevicePixelRatio(ratio)
    return scaled


def scaled_to_height_for_screen(source: QPixmap, height: int, ratio: float) -> QPixmap:
    """높이를 `height`(논리 픽셀)로 맞추되, 화면이 실제로 찍을 픽셀은 남긴다."""
    scaled = source.scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    return scaled


class HiDpiImageLabel(QLabel):
    """이미지를 직접 그리는 라벨 — 모듈 docstring 참조."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image: QPixmap | None = None

    @property
    def image(self) -> QPixmap | None:
        """현재 들고 있는 픽스맵 (화면 배율만큼의 실제 픽셀 + 배율 태그)."""
        return self._image

    def set_image(self, pixmap: QPixmap | None) -> None:
        """이미 배율에 맞춰 스케일된 픽스맵을 앉힌다."""
        self._image = pixmap
        if pixmap is not None:
            super().setText("")
        self.update()

    def show_fitted(self, source: QPixmap, box: QSize) -> None:
        """원본을 `box`(논리 픽셀) 안에 맞춰 보여준다."""
        self.set_image(scaled_for_screen(source, box, self.devicePixelRatioF()))

    def show_at_height(self, source: QPixmap, height: int) -> None:
        """원본을 높이 `height`(논리 픽셀)에 맞춰 보여준다."""
        self.set_image(scaled_to_height_for_screen(source, height, self.devicePixelRatioF()))

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 관례)
        self._image = None
        super().setText(text)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._image is None:
            super().paintEvent(event)
            return
        # 논리 크기 — 픽스맵은 그보다 배율만큼 많은 픽셀을 담고 있다
        size = self._image.deviceIndependentSize()
        painter = QPainter(self)
        painter.drawPixmap(
            QPointF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2),
            self._image,
        )
        painter.end()


__all__ = ["HiDpiImageLabel", "scaled_for_screen", "scaled_to_height_for_screen"]
