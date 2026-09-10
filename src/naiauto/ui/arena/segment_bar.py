"""분포를 가로 막대 하나로 — 티어와 세대가 쓴다.

예전에는 `티어 분포 — S 0 · A 0 · B 0 · C 0 · D 0 · F 0` 처럼 한 줄짜리 글이었다.
숫자를 하나하나 읽어야 어느 쪽으로 쏠렸는지 알 수 있었는데, 이건 훑어보라고 있는
정보다.

칸마다 배경색을 주고 **개수만큼 폭을 나눠 갖게** 한다. 0인 칸은 폭을 갖지 않으므로
저절로 사라진다. 색은 `style.ramp()`가 팔레트의 강조색에서 투명도만 깎아 만든다 —
색을 새로 지어내지 않으므로 어떤 테마에서도 한 계통으로 읽힌다.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QWidget

from .style import ramp

#: 막대 높이 (논리 픽셀). 글 한 줄보다 낮게 — 정보의 무게에 맞춘다.
BAR_HEIGHT = 10


class SegmentBar(QWidget):
    """개수에 비례해 폭을 나눠 갖는 가로 막대."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[QFrame] = []
        self.setFixedHeight(BAR_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self._layout = layout

    @property
    def segment_count(self) -> int:
        """지금 들고 있는 칸 수 (세대는 늘어나므로 고정이 아니다)."""
        return len(self._segments)

    def set_counts(self, counts: list[int], labels: list[str] | None = None) -> None:
        """칸마다 개수를 준다. 전부 0이면 막대를 비운다."""
        self._ensure_segments(len(counts))
        colours = ramp(self.palette(), len(counts))
        for index, (segment, count) in enumerate(zip(self._segments, counts, strict=True)):
            segment.setStyleSheet(f"background-color: {colours[index]}; border: none;")
            # 폭은 개수에 비례한다 — 0이면 자리를 갖지 않아 저절로 사라진다.
            self._layout.setStretch(index, max(0, count))
            if labels is not None and index < len(labels):
                segment.setToolTip(f"{labels[index]} {count}")
        self.setVisible(any(count > 0 for count in counts))

    def _ensure_segments(self, wanted: int) -> None:
        """칸 개수를 맞춘다 — 세대는 늘어나므로 매번 같지 않다."""
        while len(self._segments) < wanted:
            segment = QFrame(self)
            segment.setFrameShape(QFrame.Shape.NoFrame)
            self._layout.addWidget(segment)
            self._segments.append(segment)
        while len(self._segments) > wanted:
            segment = self._segments.pop()
            self._layout.removeWidget(segment)
            segment.setParent(None)


__all__ = ["BAR_HEIGHT", "SegmentBar"]
