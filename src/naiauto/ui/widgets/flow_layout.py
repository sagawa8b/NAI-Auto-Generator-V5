"""가로로 채우다 자리가 모자라면 다음 줄로 넘기는 레이아웃.

좌측 입력 패널을 창 절반 폭까지 좁힐 수 있게 하려고 만들었다. `QHBoxLayout`은
한 줄에 들어가는 위젯들의 최소 폭을 전부 더한 값이 그대로 패널의 최소 폭이 되어,
버튼이 네 개만 있어도 패널이 그 아래로 줄지 않는다. 이 레이아웃은 폭이 모자라면
줄을 바꾸므로 최소 폭이 "가장 넓은 위젯 하나"로 내려간다.

`expand=True`면 한 줄에 남은 자리를 그 줄의 위젯들이 나눠 갖는다 (생성 버튼처럼
가로를 꽉 채우던 줄이 좁아졌다고 왼쪽에 몰리지 않게). `expand=False`면 위젯은
제 폭 그대로 왼쪽부터 놓인다 (`addStretch(1)`로 끝나던 줄과 같은 모습).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget

#: 위젯 사이 기본 간격 (px). Qt 기본 레이아웃 간격과 같은 값을 쓴다.
DEFAULT_SPACING = 6


class FlowLayout(QLayout):
    """폭이 모자라면 다음 줄로 넘기는 레이아웃."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        spacing: int = DEFAULT_SPACING,
        expand: bool = False,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._expand = expand
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # ── QLayout 필수 구현 ────────────────────────────────

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt 콜백)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    # ── 크기 ─────────────────────────────────────────────

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        """한 줄에 다 놓았을 때의 크기 — 넓을 때는 지금까지와 같은 모습이 된다."""
        width = 0
        height = 0
        for index, item in enumerate(self._items):
            hint = item.sizeHint()
            width += hint.width() + (self.spacing() if index else 0)
            height = max(height, hint.height())
        return self._with_margins(QSize(width, height))

    def minimumSize(self) -> QSize:  # noqa: N802
        """가장 넓은 위젯 하나가 들어갈 폭 — 나머지는 줄을 바꿔 담는다."""
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return self._with_margins(size)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def _with_margins(self, size: QSize) -> QSize:
        margins = self.contentsMargins()
        return QSize(
            size.width() + margins.left() + margins.right(),
            size.height() + margins.top() + margins.bottom(),
        )

    # ── 배치 ─────────────────────────────────────────────

    def _rows(self, width: int) -> list[list[QLayoutItem]]:
        """주어진 폭에 맞춰 위젯을 줄 단위로 나눈다."""
        rows: list[list[QLayoutItem]] = []
        current: list[QLayoutItem] = []
        used = 0
        for item in self._items:
            item_width = item.sizeHint().width()
            needed = item_width + (self.spacing() if current else 0)
            if current and used + needed > width:
                rows.append(current)
                current = [item]
                used = item_width
                continue
            current.append(item)
            used += needed
        if current:
            rows.append(current)
        return rows

    def _do_layout(self, rect: QRect, *, apply: bool) -> int:
        """줄을 나눠 배치하고 전체 높이를 돌려준다. `apply=False`면 계산만 한다."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        y = area.y()
        for index, row in enumerate(self._rows(max(0, area.width()))):
            if index:
                y += self.spacing()
            y += self._place_row(row, area, y, apply=apply)
        return y - rect.y() + margins.bottom()

    def _place_row(self, row: list[QLayoutItem], area: QRect, y: int, *, apply: bool) -> int:
        """한 줄을 놓고 그 줄의 높이를 돌려준다."""
        widths = [item.sizeHint().width() for item in row]
        if self._expand:
            widths = self._grown(row, widths, area.width())
        height = max(item.sizeHint().height() for item in row)
        if apply:
            x = area.x()
            for item, width in zip(row, widths, strict=True):
                item.setGeometry(QRect(QPoint(x, y), QSize(width, height)))
                x += width + self.spacing()
        return height

    def _grown(self, row: list[QLayoutItem], widths: list[int], available: int) -> list[int]:
        """남은 자리를 늘어날 수 있는 위젯들이 고르게 나눠 갖는다.

        줄 안에 Expanding 위젯(콤보 상자 등)이 있으면 그것만 늘린다 — QHBoxLayout에서
        `stretch=1`을 준 것과 같은 모습이 된다. 없으면 늘어날 수 있는 위젯 전부가
        나눠 갖는다 (버튼 네 개가 가로를 꽉 채우던 줄).
        """
        spare = available - sum(widths) - self.spacing() * (len(row) - 1)
        growable = [
            i for i, item in enumerate(row) if self._has_flag(item, QSizePolicy.PolicyFlag.ExpandFlag)
        ]
        if not growable:
            growable = [
                i for i, item in enumerate(row) if self._has_flag(item, QSizePolicy.PolicyFlag.GrowFlag)
            ]
        if spare <= 0 or not growable:
            return widths
        share, remainder = divmod(spare, len(growable))
        for order, index in enumerate(growable):
            widths[index] += share + (1 if order < remainder else 0)
        return widths

    @staticmethod
    def _has_flag(item: QLayoutItem, flag: QSizePolicy.PolicyFlag) -> bool:
        widget = item.widget()
        if widget is None:
            return False
        # Policy와 PolicyFlag는 서로 다른 열거형이라 값끼리 비교해야 한다
        # (`&`로 바로 묶으면 TypeError가 나고, Qt가 부르는 가상 함수 안이라 그대로 죽는다).
        return bool(widget.sizePolicy().horizontalPolicy().value & flag.value)


__all__ = ["DEFAULT_SPACING", "FlowLayout"]
