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

## 넣는 자리를 오른쪽으로 뺀다

표가 화면 전체를 차지하는데 정작 **작가를 넣는 입구가 표 밑에 눌려** 있었다. 끌어다
놓기는 가장 편한 방법인데 회색 안내문 한 줄로만 존재해, 그런 기능이 있는 줄도 알기
어려웠다.

- 왼쪽은 순위표, 오른쪽은 넣는 자리(과녁 · 붙여넣기 · 추가 버튼)로 나눈다.
- 끌어다 놓는 자리를 **실제로 보이는 점선 과녁**으로 만들고, 끌고 오는 동안 밝힌다.
  창 전체 드롭도 그대로 받는다 — 과녁을 정확히 겨눌 필요는 없다.
- 후보가 수백 명이 되면 눈으로 못 찾는다 — **이름 검색**을 붙였다.
- 되돌릴 수 없는 `명단 비우기`는 `선택 삭제`와 떼어 놓고 위험 등급을 준다.
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
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
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
from .style import ROLE_DROP_ZONE, mark_danger, mark_primary

logger = logging.getLogger(__name__)

COL_NAME = 0
COL_LOCK = 1
COL_ELO = 2
COL_WINRATE = 3
COL_USES = 4
COL_NOTE = 5  # 사용자 메모 (편집 가능) — 맨 오른쪽에 둬서 기존 열 인덱스를 안 흔든다
COLUMN_COUNT = 6

SORT_NAME = "name"
SORT_ELO = "elo"
SORT_USES = "uses"
SORT_WINRATE = "winrate"
SORT_ORDERS = (SORT_ELO, SORT_WINRATE, SORT_NAME, SORT_USES)

#: 끌어다 놓기를 받는 확장자. 판독기가 열 수 있는 것만.
_IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")

#: 한 번에 너무 많이 떨구면 판독에 시간이 걸린다 — 여기까지만 본다.
_MAX_DROPPED_FILES = 500

#: 좌우 패널 사이의 여백과 처음 뜰 때의 나눔 비율 (논리 픽셀).
PANEL_GAP = 8
SPLIT_SIZES = (620, 300)

#: 끌어다 놓기를 받는 과녁의 최소 높이 (논리 픽셀).
DROP_ZONE_HEIGHT = 96


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

        root = QVBoxLayout(self)
        # 표가 화면 전체를 차지하는데 정작 **작가를 넣는 입구가 표 밑에 눌려** 있었다.
        # 왼쪽은 순위표, 오른쪽은 넣는 자리.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, 1)

        # ── 왼쪽: 순위표 ────────────────────────────────────────────────
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, PANEL_GAP, 0)

        top = QHBoxLayout()
        self.count_label = QLabel()
        top.addWidget(self.count_label)
        # 후보가 수백 명이 되면 눈으로 찾기 어렵다.
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.refresh)
        top.addWidget(self.search_edit, 1)
        self.sort_label = QLabel()
        top.addWidget(self.sort_label)
        self.sort_combo = QComboBox()
        for order in SORT_ORDERS:
            self.sort_combo.addItem("", order)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        top.addWidget(self.sort_combo)
        layout.addLayout(top)

        self.table = QTableWidget(0, COLUMN_COUNT)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 확장 선택 — Ctrl/Shift로 여러 줄을 고를 수 있다 (원본은 하나씩만 지워졌다)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Stretch)
        for col in (COL_LOCK, COL_ELO, COL_WINRATE, COL_USES):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        row_actions = QHBoxLayout()
        self.remove_button = QPushButton()
        self.remove_button.clicked.connect(self.remove_selected)
        row_actions.addWidget(self.remove_button)
        self.normalize_button = QPushButton()
        self.normalize_button.clicked.connect(self._on_normalize)
        row_actions.addWidget(self.normalize_button)
        row_actions.addStretch(1)
        layout.addLayout(row_actions)
        self.splitter.addWidget(left)

        # ── 오른쪽: 넣는 자리 ───────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(PANEL_GAP, 0, 0, 0)

        # 끌어다 놓기는 가장 편한 방법인데 회색 안내문 한 줄로만 존재했다 — 실제로
        # 보이는 과녁을 만든다 (창 전체 드롭도 그대로 받는다).
        self.drop_zone = QLabel()
        self.drop_zone.setObjectName(ROLE_DROP_ZONE)
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_zone.setWordWrap(True)
        self.drop_zone.setMinimumHeight(DROP_ZONE_HEIGHT)
        right_layout.addWidget(self.drop_zone)

        self.add_edit = QPlainTextEdit()
        self.add_edit.setMinimumHeight(72)
        right_layout.addWidget(self.add_edit, 1)

        self.add_button = QPushButton()
        mark_primary(self.add_button)
        self.add_button.clicked.connect(self._on_add)
        right_layout.addWidget(self.add_button)

        right_layout.addStretch(1)
        # 되돌릴 수 없다 — 표 옆의 `선택 삭제`와 떼어 놓고 위험 등급을 준다.
        self.clear_button = QPushButton()
        mark_danger(self.clear_button)
        self.clear_button.clicked.connect(self._on_clear)
        right_layout.addWidget(self.clear_button)
        self.splitter.addWidget(right)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes(list(SPLIT_SIZES))

        self.setAcceptDrops(True)
        self.retranslate()
        self.refresh()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._updating = True
        try:
            entries = self._visible_entries()
            self.table.setRowCount(len(entries))
            for row, entry in enumerate(entries):
                self._fill_row(row, entry)
        finally:
            self._updating = False
        total = len(self.state.artists)
        if len(entries) == total:
            self.count_label.setText(self.tr("arena.roster_count").format(total))
        else:
            # 걸러 놓고 "N명"만 보이면 나머지가 지워진 줄 안다.
            self.count_label.setText(self.tr("arena.roster_count_filtered").format(len(entries), total))

    def retranslate(self) -> None:
        tr = self.tr
        self.table.setHorizontalHeaderLabels(
            [
                tr("arena.col_artist"),
                tr("arena.col_locked_weight"),
                tr("arena.col_elo"),
                tr("arena.col_winrate"),
                tr("arena.col_uses"),
                tr("arena.col_note"),
            ]
        )
        labels = {
            SORT_ELO: tr("arena.sort_elo"),
            SORT_WINRATE: tr("arena.sort_winrate"),
            SORT_NAME: tr("arena.sort_name"),
            SORT_USES: tr("arena.sort_uses"),
        }
        for index in range(self.sort_combo.count()):
            self.sort_combo.setItemText(index, labels[self.sort_combo.itemData(index)])
        self.sort_label.setText(tr("arena.sort_by"))
        self.search_edit.setPlaceholderText(tr("arena.roster_search"))
        self.add_edit.setPlaceholderText(tr("arena.roster_add_placeholder"))
        self.add_button.setText(tr("arena.roster_add"))
        self.remove_button.setText(tr("arena.roster_remove"))
        self.normalize_button.setText(tr("arena.roster_normalize"))
        self.normalize_button.setToolTip(tr("arena.roster_normalize_hint"))
        self.clear_button.setText(tr("arena.roster_clear"))
        self.drop_zone.setText(tr("arena.roster_drop_hint"))
        self.refresh()

    # ── 표 ──────────────────────────────────────────────────────────────

    def _visible_entries(self) -> list[ArtistEntry]:
        """정렬한 뒤 검색어로 거른 줄들. 검색어가 비면 전부."""
        needle = self.search_edit.text().strip().casefold()
        entries = self._sorted_entries()
        if not needle:
            return entries
        return [entry for entry in entries if needle in entry.name.casefold()]

    def _sorted_entries(self) -> list[ArtistEntry]:
        entries = list(self.state.artists)
        if self._sort == SORT_NAME:
            entries.sort(key=lambda e: e.name.casefold())
        elif self._sort == SORT_USES:
            entries.sort(key=lambda e: e.uses, reverse=True)
        elif self._sort == SORT_WINRATE:
            # 대결 기록이 없는 작가는 아래로 (0%가 아니라 '모름'이다). 승률이 같으면
            # 표본이 많은 쪽(대결 수)을 위로 둔다.
            entries.sort(key=lambda e: (e.matches > 0, e.win_rate, e.matches), reverse=True)
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

        # 메모 — 편집 가능. 셀이 좁을 때 잘리므로 전체 내용을 툴팁에도 담는다.
        note_item = QTableWidgetItem(entry.note)
        if entry.note:
            note_item.setToolTip(entry.note)
        self.table.setItem(row, COL_NOTE, note_item)

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
        elif item.column() == COL_NOTE:
            self._set_note(entry, item.text())

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

    def _set_note(self, entry: ArtistEntry, raw: str) -> None:
        """작가 메모를 저장한다. 앞뒤 공백만 다듬고 내용은 그대로 둔다."""
        note = raw.strip()
        if note == entry.note:
            return
        entry.note = note
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
            self._highlight_drop_zone(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._highlight_drop_zone(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._highlight_drop_zone(False)
        paths = self._image_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        self.add_from_images(paths)

    def _highlight_drop_zone(self, active: bool) -> None:
        """끌고 오는 동안 과녁을 밝힌다 — 여기에 놓으면 된다는 신호."""
        self.drop_zone.setProperty("dragging", "true" if active else "false")
        # 동적 속성으로 고른 QSS는 다시 계산해 줘야 반영된다.
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

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
