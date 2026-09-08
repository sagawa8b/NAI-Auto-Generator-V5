"""작가 명단 탭 — 조합에 뽑힐 후보를 관리한다.

한 줄이 작가 한 명이고, 조합이 대결할 때마다 그 조합에 낀 작가도 점수를 받으므로
여기가 곧 "내가 좋아하는 작가 순위표"가 된다.

커뮤니티에서 나온 요청을 그대로 반영한 곳이다:

- 여러 줄을 골라 한 번에 지운다 (원본은 하나씩만 지워졌다). `Delete` 키도 먹는다.
- 작가명을 표에서 바로 고친다 (`_`가 들어간 이름을 다듬고 싶다는 요청).
- 가중치가 붙은 완성 프롬프트를 통째로 붙여 넣어도 작가 태그만 골라 등록한다.
- **이미지를 끌어다 놓으면 그 그림에 쓰인 작가를 뽑아 등록한다.** 원본 제작자는
  webp의 메타데이터를 읽지 못해 이 기능을 접었는데, 이 앱은 PNG tEXt·webp EXIF·
  스텔스 메타데이터를 모두 읽는 판독기(`core/metadata/naiinfo.py`)가 이미 있다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import extract_artist_names, normalize_artist_name, underscores_to_spaces
from ...core.arena.models import ArtistEntry
from ...core.metadata.naiinfo import read_metadata
from ...core.metadata.reuse import extract_reusable
from .base import ArenaTab

logger = logging.getLogger(__name__)

COL_NAME = 0
COL_LOCK = 1
COL_ELO = 2
COL_WINRATE = 3
COL_USES = 4
COLUMN_COUNT = 5

SORT_NAME = "name"
SORT_ELO = "elo"
SORT_USES = "uses"
SORT_ORDERS = (SORT_ELO, SORT_NAME, SORT_USES)

#: 끌어다 놓기를 받는 확장자. 판독기가 열 수 있는 것만.
_IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")

#: 한 번에 너무 많이 떨구면 판독에 시간이 걸린다 — 여기까지만 본다.
_MAX_DROPPED_FILES = 500


def artists_in_image(path: Path) -> list[str]:
    """이미지 한 장의 메타데이터에서 작가 이름을 뽑는다. 못 읽으면 빈 목록.

    프롬프트만 본다 — 네거티브에 들어간 작가는 "쓰지 않겠다"는 뜻이라 명단에 넣으면
    반대가 된다.
    """
    try:
        metadata = read_metadata(path)
    except Exception as e:  # 판독기는 관대하지만, 깨진 파일 하나가 드롭 전체를 막으면 안 된다
        logger.debug("cannot read metadata from %s: %s", path, e)
        return []
    if not metadata:
        return []
    # `artist:` 접두사를 강제한다 — 메타데이터에 든 것은 완성 프롬프트라, 되돌림을
    # 허용하면 `1girl`·`solo` 같은 태그가 전부 작가로 둔갑한다.
    return extract_artist_names(extract_reusable(metadata).prompt, require_prefix=True)


class RosterTab(ArenaTab):
    """작가 명단."""

    KEY = "roster"

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)
        self._updating = False  # 표를 다시 그리는 동안 itemChanged 재진입 방지
        self._sort = SORT_ELO

        layout = QVBoxLayout(self)

        # 1행: 개수 + 정렬
        top = QHBoxLayout()
        self.count_label = QLabel()
        top.addWidget(self.count_label)
        top.addStretch(1)
        self.sort_label = QLabel()
        top.addWidget(self.sort_label)
        self.sort_combo = QComboBox()
        for order in SORT_ORDERS:
            self.sort_combo.addItem("", order)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        top.addWidget(self.sort_combo)
        layout.addLayout(top)

        # 표
        self.table = QTableWidget(0, COLUMN_COUNT)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 확장 선택 — Ctrl/Shift로 여러 줄을 고를 수 있다 (원본은 하나씩만 지워졌다)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (COL_LOCK, COL_ELO, COL_WINRATE, COL_USES):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        # 붙여넣기 입력
        self.add_edit = QPlainTextEdit()
        self.add_edit.setMaximumHeight(72)
        layout.addWidget(self.add_edit)

        # 버튼 줄
        buttons = QHBoxLayout()
        self.add_button = QPushButton()
        self.add_button.clicked.connect(self._on_add)
        buttons.addWidget(self.add_button)
        self.remove_button = QPushButton()
        self.remove_button.clicked.connect(self.remove_selected)
        buttons.addWidget(self.remove_button)
        self.normalize_button = QPushButton()
        self.normalize_button.clicked.connect(self._on_normalize)
        buttons.addWidget(self.normalize_button)
        buttons.addStretch(1)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self._on_clear)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.setAcceptDrops(True)
        self.retranslate()
        self.refresh()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._updating = True
        try:
            entries = self._sorted_entries()
            self.table.setRowCount(len(entries))
            for row, entry in enumerate(entries):
                self._fill_row(row, entry)
        finally:
            self._updating = False
        self.count_label.setText(self.tr("arena.roster_count").format(len(self.state.artists)))

    def retranslate(self) -> None:
        tr = self.tr
        self.table.setHorizontalHeaderLabels(
            [
                tr("arena.col_artist"),
                tr("arena.col_locked_weight"),
                tr("arena.col_elo"),
                tr("arena.col_winrate"),
                tr("arena.col_uses"),
            ]
        )
        labels = {
            SORT_ELO: tr("arena.sort_elo"),
            SORT_NAME: tr("arena.sort_name"),
            SORT_USES: tr("arena.sort_uses"),
        }
        for index in range(self.sort_combo.count()):
            self.sort_combo.setItemText(index, labels[self.sort_combo.itemData(index)])
        self.sort_label.setText(tr("arena.sort_by"))
        self.add_edit.setPlaceholderText(tr("arena.roster_add_placeholder"))
        self.add_button.setText(tr("arena.roster_add"))
        self.remove_button.setText(tr("arena.roster_remove"))
        self.normalize_button.setText(tr("arena.roster_normalize"))
        self.clear_button.setText(tr("arena.roster_clear"))
        self.hint_label.setText(tr("arena.roster_drop_hint"))
        self.refresh()

    # ── 표 ──────────────────────────────────────────────────────────────

    def _sorted_entries(self) -> list[ArtistEntry]:
        entries = list(self.state.artists)
        if self._sort == SORT_NAME:
            entries.sort(key=lambda e: e.name.casefold())
        elif self._sort == SORT_USES:
            entries.sort(key=lambda e: e.uses, reverse=True)
        else:  # SORT_ELO — 대결 기록이 없는 작가는 아래로 (1000점은 실력이 아니라 '모름')
            entries.sort(key=lambda e: (e.matches > 0, e.elo), reverse=True)
        return entries

    def _fill_row(self, row: int, entry: ArtistEntry) -> None:
        name_item = QTableWidgetItem(entry.name)
        # 표를 다시 그려도 어느 항목의 줄인지 잃지 않게 이름을 데이터로 붙여 둔다.
        name_item.setData(Qt.ItemDataRole.UserRole, entry.name)
        self.table.setItem(row, COL_NAME, name_item)

        lock_text = "" if entry.locked_weight is None else f"{entry.locked_weight:g}"
        lock_item = QTableWidgetItem(lock_text)
        lock_item.setToolTip(self.tr("arena.locked_weight_hint"))
        self.table.setItem(row, COL_LOCK, lock_item)

        for col, text in (
            (COL_ELO, f"{entry.elo:.0f}" if entry.matches else "-"),
            (
                COL_WINRATE,
                f"{entry.win_rate * 100:.0f}% ({entry.wins}/{entry.matches})" if entry.matches else "-",
            ),
            (COL_USES, str(entry.uses)),
        ):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, item)

    def _entry_for_row(self, row: int) -> ArtistEntry | None:
        item = self.table.item(row, COL_NAME)
        if item is None:
            return None
        return self.state.artist(str(item.data(Qt.ItemDataRole.UserRole)))

    def _on_sort_changed(self) -> None:
        self._sort = self.sort_combo.currentData() or SORT_ELO
        self.refresh()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        entry = self._entry_for_row(item.row())
        if entry is None:
            return
        if item.column() == COL_NAME:
            self._rename(entry, item.text())
        elif item.column() == COL_LOCK:
            self._set_locked_weight(entry, item.text())

    def _rename(self, entry: ArtistEntry, raw: str) -> None:
        """표에서 이름을 고친다. 빈 이름이나 중복은 되돌린다."""
        name = normalize_artist_name(raw)
        if not name or name == entry.name:
            self.refresh()
            return
        if any(other.name == name for other in self.state.artists if other is not entry):
            self.status_message.emit(self.tr("arena.roster_duplicate").format(name))
            self.refresh()
            return
        entry.name = name
        self._changed()

    def _set_locked_weight(self, entry: ArtistEntry, raw: str) -> None:
        """빈 칸이면 고정 해제, 숫자면 그 값으로 고정."""
        text = raw.strip()
        if not text:
            entry.locked_weight = None
        else:
            try:
                entry.locked_weight = float(text)
            except ValueError:
                self.status_message.emit(self.tr("arena.roster_bad_weight").format(text))
                self.refresh()
                return
        self._changed()

    # ── 동작 ────────────────────────────────────────────────────────────

    def add_names(self, names: list[str]) -> int:
        """이름 목록을 명단에 넣고 실제로 추가된 수를 돌려준다."""
        added = self.state.add_artists(names)
        if added:
            self._changed()
        return added

    def _on_add(self) -> None:
        text = self.add_edit.toPlainText()
        names = extract_artist_names(text)
        if not names:
            self.status_message.emit(self.tr("arena.roster_nothing_to_add"))
            return
        added = self.add_names(names)
        self.add_edit.clear()
        self.status_message.emit(self.tr("arena.roster_added").format(added, len(names) - added))

    def selected_names(self) -> list[str]:
        """고른 줄들의 작가 이름 (표시 순서대로, 중복 없이)."""
        names: list[str] = []
        for index in sorted({item.row() for item in self.table.selectedItems()}):
            entry = self._entry_for_row(index)
            if entry is not None and entry.name not in names:
                names.append(entry.name)
        return names

    def remove_selected(self) -> None:
        names = self.selected_names()
        if not names:
            self.status_message.emit(self.tr("arena.roster_no_selection"))
            return
        removed = self.state.remove_artists(set(names))
        if removed:
            self._changed()
        self.status_message.emit(self.tr("arena.roster_removed").format(removed))

    def _on_normalize(self) -> None:
        """`kantoku_(artist)` → `kantoku (artist)`. 겹치는 이름이 생기면 그 줄은 둔다."""
        changed = 0
        for entry in self.state.artists:
            renamed = underscores_to_spaces(entry.name)
            if renamed == entry.name:
                continue
            if any(other.name == renamed for other in self.state.artists if other is not entry):
                continue
            entry.name = renamed
            changed += 1
        if changed:
            self._changed()
        self.status_message.emit(self.tr("arena.roster_normalized").format(changed))

    def _on_clear(self) -> None:
        if not self.state.artists:
            return
        answer = QMessageBox.question(
            self,
            self.tr("arena.roster_clear"),
            self.tr("arena.roster_clear_confirm").format(len(self.state.artists)),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.state.remove_artists({entry.name for entry in self.state.artists})
        self._changed()
        self.status_message.emit(self.tr("arena.roster_removed").format(removed))

    def _changed(self) -> None:
        self.refresh()
        self.state_changed.emit()

    # ── 끌어다 놓기 ─────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt 콜백 이름)
        if any(self._image_paths(event)):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = self._image_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        self.add_from_images(paths)

    def add_from_images(self, paths: list[Path]) -> None:
        """떨어뜨린 그림들에서 작가를 뽑아 명단에 넣는다.

        메타데이터가 없거나 작가 태그가 없는 그림은 조용히 지나간다 — 몇 장에서
        찾았는지를 상태줄에 알린다.
        """
        names: list[str] = []
        read = 0
        for path in paths[:_MAX_DROPPED_FILES]:
            found = artists_in_image(path)
            if found:
                read += 1
            for name in found:
                if name not in names:
                    names.append(name)
        if not names:
            self.status_message.emit(self.tr("arena.roster_drop_none").format(len(paths)))
            return
        added = self.add_names(names)
        self.status_message.emit(self.tr("arena.roster_drop_added").format(added, read))

    @staticmethod
    def _image_paths(event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in _IMAGE_SUFFIXES:
                paths.append(Path(local))
        return paths

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """표에서 Delete를 누르면 고른 줄을 지운다 (단축키 요청)."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.table.hasFocus():
            self.remove_selected()
            return
        super().keyPressEvent(event)


__all__ = ["RosterTab", "artists_in_image"]
