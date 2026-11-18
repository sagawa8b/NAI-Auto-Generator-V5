"""결과 히스토리 줄 — 결과 이미지 아래의 최근 결과 썸네일 한 줄.

상태(목록·커서·따라가기 규칙)는 `core/result_history.ResultHistory`가 갖고, 이 위젯은
그것을 그리고 클릭을 알리기만 한다.

썸네일은 **추가될 때 한 번** 줄여서 아이콘으로 넣는다. 결과 이미지는 한 장에 수 MB라,
그릴 때마다 원본을 읽으면 연속 생성 중에 줄 전체가 버벅인다.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QListWidget, QListWidgetItem, QWidget

THUMB_SIZE = 64
#: 줄의 높이 — 썸네일 + 가로 스크롤 막대가 들어갈 자리.
STRIP_HEIGHT = THUMB_SIZE + 30


def _dropped_from_front(shown: list[str], wanted: list[str]) -> int:
    """`shown`의 앞에서 몇 개를 빼면 남은 것이 `wanted`의 앞부분과 겹치는가.

    `shown=[a,b,c]`, `wanted=[b,c,d]` → 1 (a를 빼고 d를 붙이면 된다). 겹치는 데가 없으면
    (중간이 빠졌거나 전혀 다른 목록) 전부 빼게 된다 — 그러면 처음부터 다시 만드는 셈이다.
    """
    for dropped in range(len(shown)):
        rest = shown[dropped:]
        if rest == wanted[: len(rest)]:
            return dropped
    return len(shown)


class HistoryStrip(QListWidget):
    """가로로 흐르는 썸네일 목록.

    Signals:
        entry_selected(int): 사용자가 그 자리의 썸네일을 골랐다 (클릭·방향키).
        entry_activated(int): 더블클릭·Enter — 이미지 정보를 열어 달라는 뜻이다.
    """

    entry_selected = Signal(int)
    entry_activated = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setGridSize(QSize(THUMB_SIZE + 8, THUMB_SIZE + 8))
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setFixedHeight(STRIP_HEIGHT)
        self.currentRowChanged.connect(self._on_row_changed)
        self.itemActivated.connect(lambda item: self.entry_activated.emit(self.row(item)))
        #: 코드가 커서를 옮기는 중인가 — 그때의 `currentRowChanged`는 사용자 조작이 아니다.
        self._syncing = False

    def set_entries(self, paths: list[str], cursor: int) -> None:
        """목록을 `paths`에 맞춘다. 이미 있는 썸네일은 다시 만들지 않는다.

        히스토리는 끝에 붙고 앞에서 빠지므로(한도 초과), 앞에서부터 어긋난 것을 지우고
        모자란 것을 뒤에 붙이면 된다.
        """
        self._syncing = True
        try:
            for _ in range(_dropped_from_front(self.paths(), paths)):
                self.takeItem(0)
            for path in paths[self.count() :]:
                self._append(path)
            self.setCurrentRow(cursor)
        finally:
            self._syncing = False
        current = self.currentItem()
        if current is not None:
            self.scrollToItem(current)

    def paths(self) -> list[str]:
        """지금 줄에 놓인 경로들 (왼쪽부터)."""
        return [self.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.count())]

    def _append(self, path: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            item.setIcon(
                QIcon(
                    pixmap.scaled(
                        THUMB_SIZE * 2,
                        THUMB_SIZE * 2,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            )
        self.addItem(item)

    def _on_row_changed(self, row: int) -> None:
        if not self._syncing and row >= 0:
            self.entry_selected.emit(row)


__all__ = ["STRIP_HEIGHT", "THUMB_SIZE", "HistoryStrip"]
