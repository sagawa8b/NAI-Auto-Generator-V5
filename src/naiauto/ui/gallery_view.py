"""갤러리 뷰 — 생성 이미지 썸네일 그리드 + 메타데이터 조회.

Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9:
  - 150×150 썸네일 스크롤 그리드
  - 클릭 → 전체 이미지 + 파싱된 PNG 메타데이터 표시
  - 뷰포트 + 1 스크린 버퍼 레이지 로딩
  - 생성 중 자동 추가 (수동 리프레시 불필요)
  - 생성일(기본, 최신 먼저) / 파일명(알파벳 오름차순) 정렬
  - 우클릭 컨텍스트 메뉴: Open in Explorer, Copy Seed, Reuse Settings
  - 빈 디렉토리 자리 표시 메시지
  - 읽을 수 없는 PNG는 깨진-이미지 자리 표시

v0.20에서 더한 것:
  - 검색 — 파일 이름과 프롬프트(메타데이터)에서 찾는다 (`core/gallery_search.py`)
  - 즐겨찾기 — 별 표시, `즐겨찾기만` 필터
  - 여러 장 선택(Ctrl·Shift) → 휴지통으로 삭제, 즐겨찾기 일괄 지정
  - 고른 이미지를 i2i·강화 패널로 보내기

구현:
  - QListView + QAbstractListModel + QStyledItemDelegate 패턴
  - 썸네일은 뷰포트 + 1 화면 버퍼 내에서만 QPixmap으로 로드
  - LRU 캐시로 메모리 제한
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPointF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..core.gallery_search import FavoriteStore, matches, searchable_text
from ..core.i18n.manager import I18nManager
from ..core.metadata.naiinfo import read_metadata
from ..core.metadata.reuse import extract_reusable
from ..core.trash import send_to_trash
from .widgets.hidpi_image import scaled_for_screen
from .widgets.window_chrome import enable_window_controls

logger = logging.getLogger(__name__)

_THUMB_SIZE = 150
_MAX_CACHE = 200
#: WebP 저장 옵션으로 생긴 결과도 함께 보여준다 (core.metadata.save.IMAGE_FORMATS).
_IMAGE_SUFFIXES = frozenset({".png", ".webp"})
#: 검색어를 치는 동안 매 글자마다 걸러 내지 않도록 잠깐 기다린다.
_SEARCH_DEBOUNCE_MS = 250
#: 백그라운드에서 읽은 프롬프트를 화면에 반영하는 주기.
_INDEX_POLL_MS = 200


class _ImageEntry:
    """단일 이미지 항목의 메타데이터 (경로 + mtime)."""

    __slots__ = ("path", "mtime", "filename")

    def __init__(self, path: str) -> None:
        self.path = path
        p = Path(path)
        self.filename = p.name
        try:
            self.mtime = p.stat().st_mtime
        except OSError:
            self.mtime = 0.0


class GalleryModel(QAbstractListModel):
    """갤러리 이미지 목록 모델 — 경로 기반, 정렬·필터 내장.

    `_all`이 폴더의 전체 목록이고 `_entries`가 지금 보이는(필터를 통과한) 목록이다.
    뷰가 보는 것은 언제나 `_entries`다.
    """

    PathRole = Qt.ItemDataRole.UserRole + 1
    FavoriteRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._all: list[_ImageEntry] = []
        self._entries: list[_ImageEntry] = []
        self._sort_order: Literal["date", "name"] = "date"
        #: 항목을 보일지 정하는 함수. None이면 전부 보인다.
        self._filter: Callable[[_ImageEntry], bool] | None = None
        #: 즐겨찾기 여부를 묻는 함수 (뷰가 `FavoriteStore`를 물려 준다).
        self._is_favorite: Callable[[str], bool] = lambda _path: False

    # --- QAbstractListModel interface ---

    _INVALID_PARENT = QModelIndex()

    def rowCount(self, parent: QModelIndex = _INVALID_PARENT) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.filename
        if role == self.PathRole:
            return entry.path
        if role == self.FavoriteRole:
            return self._is_favorite(entry.path)
        return None

    # --- Public API ---

    def set_entries(self, paths: list[str]) -> None:
        """전체 목록 교체 (refresh 용)."""
        self.beginResetModel()
        self._all = [_ImageEntry(p) for p in paths]
        self._rebuild()
        self.endResetModel()

    def append_image(self, path: str) -> None:
        """새 이미지 추가. 정렬 기준에 따라 적절한 위치에 삽입 (필터에 걸리면 숨긴 채로)."""
        entry = _ImageEntry(path)
        self._all.insert(self._insert_position(entry, self._all), entry)
        if self._filter is not None and not self._filter(entry):
            return
        pos = self._insert_position(entry)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._entries.insert(pos, entry)
        self.endInsertRows()

    def remove_paths(self, paths: list[str]) -> None:
        """지운 파일들을 목록에서 뺀다."""
        gone = set(paths)
        self.beginResetModel()
        self._all = [entry for entry in self._all if entry.path not in gone]
        self._entries = [entry for entry in self._entries if entry.path not in gone]
        self.endResetModel()

    def set_sort_order(self, order: Literal["date", "name"]) -> None:
        """정렬 기준 변경 후 전체 재정렬."""
        if order == self._sort_order:
            return
        self._sort_order = order
        self.beginResetModel()
        self._rebuild()
        self.endResetModel()

    @property
    def sort_order(self) -> Literal["date", "name"]:
        return self._sort_order

    def set_filter(self, predicate: Callable[[_ImageEntry], bool] | None) -> None:
        """보일 항목을 다시 고른다 (검색어·즐겨찾기 필터가 바뀔 때)."""
        self._filter = predicate
        self.refilter()

    def refilter(self) -> None:
        """같은 필터로 다시 거른다 — 필터가 보는 값(프롬프트 색인·즐겨찾기)이 바뀌었을 때."""
        self.beginResetModel()
        self._rebuild()
        self.endResetModel()

    def set_favorite_lookup(self, lookup: Callable[[str], bool]) -> None:
        self._is_favorite = lookup

    def favorites_changed(self) -> None:
        """별 표시를 다시 그리게 한다 (목록 자체는 그대로)."""
        if self._entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._entries) - 1, 0))

    def path_at(self, index: QModelIndex) -> str | None:
        """주어진 인덱스의 파일 경로 반환."""
        if not index.isValid() or index.row() >= len(self._entries):
            return None
        return self._entries[index.row()].path

    def all_paths(self) -> list[str]:
        """필터와 무관하게 폴더의 전체 경로."""
        return [entry.path for entry in self._all]

    @property
    def total_count(self) -> int:
        return len(self._all)

    # --- Internal ---

    def _rebuild(self) -> None:
        """전체 목록을 정렬하고 보이는 목록을 다시 만든다."""
        self._all.sort(key=self._sort_key(), reverse=self._sort_order == "date")
        if self._filter is None:
            self._entries = list(self._all)
        else:
            self._entries = [entry for entry in self._all if self._filter(entry)]

    def _sort_key(self):
        if self._sort_order == "date":
            return lambda e: e.mtime
        return lambda e: e.filename.lower()

    def _apply_sort(self) -> None:
        """보이는 목록만 정렬한다 (필터는 그대로)."""
        self._entries.sort(key=self._sort_key(), reverse=self._sort_order == "date")

    def _insert_position(self, entry: _ImageEntry, entries: list[_ImageEntry] | None = None) -> int:
        """이진 탐색으로 정렬 유지하는 삽입 위치 결정."""
        entries = self._entries if entries is None else entries
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._comes_before(entry, entries[mid]):
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _comes_before(self, a: _ImageEntry, b: _ImageEntry) -> bool:
        """a가 b 앞에 와야 하면 True."""
        if self._sort_order == "date":
            return a.mtime > b.mtime  # 최신 먼저
        return a.filename.lower() < b.filename.lower()  # 알파벳 오름차순


class _ThumbnailDelegate(QStyledItemDelegate):
    """150×150 썸네일을 lazy-load하여 렌더링하는 커스텀 델리게이트.

    뷰포트에 보이는 항목만 QPixmap을 로드하고, LRU 캐시로 메모리를 제한한다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 키에 화면 배율을 넣는다 — 창을 배율이 다른 모니터로 옮기면 새로 만들어야 한다
        self._cache: dict[tuple[str, float], QPixmap] = {}
        self._cache_order: list[tuple[str, float]] = []
        self._broken_placeholder: QPixmap | None = None

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(_THUMB_SIZE, _THUMB_SIZE)

    def paint(self, painter, option, index) -> None:
        painter.save()
        # Background.
        # `QStyle.StateFlag.State_Selected`로 쓴다 — `option.State_Selected`는 PySide6의
        # QStyleOptionViewItem에 없다. 그렇게 쓰면 셀마다 AttributeError가 나고 PySide6가
        # 그 예외를 stderr로만 흘려 보내, 그리기가 통째로 멈춘 빈 흰 격자가 남는다 (v0.2.4).
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        path = index.data(GalleryModel.PathRole)
        if path:
            pixmap = self._get_thumbnail(path, painter.device().devicePixelRatioF())
            if pixmap and not pixmap.isNull():
                # 셀 안에 가운데 정렬 — 픽스맵은 배율만큼 큰 실제 픽셀을 담고 있으므로
                # 자리 계산에는 논리 크기를 쓴다
                size = pixmap.deviceIndependentSize()
                x = option.rect.x() + (option.rect.width() - size.width()) / 2
                y = option.rect.y() + (option.rect.height() - size.height()) / 2
                painter.drawPixmap(QPointF(x, y), pixmap)
            else:
                # Broken image placeholder
                self._draw_broken_placeholder(painter, option.rect)
        if index.data(GalleryModel.FavoriteRole):
            self._draw_favorite_mark(painter, option.rect)
        painter.restore()

    @staticmethod
    def _draw_favorite_mark(painter, rect) -> None:
        """즐겨찾기 별 — 셀 오른쪽 위. 어떤 그림 위에서도 보이게 어두운 바탕을 깐다."""
        size = 22
        badge = rect.adjusted(rect.width() - size - 4, 4, -4, -(rect.height() - size - 4))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(QColor("#ffd54a"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "★")

    def _get_thumbnail(self, path: str, ratio: float) -> QPixmap | None:
        """캐시에서 썸네일을 가져오거나, 없으면 로드."""
        key = (path, ratio)
        if key in self._cache:
            # Move to end (most recently used)
            self._cache_order.remove(key)
            self._cache_order.append(key)
            return self._cache[key]

        # Load and scale
        pixmap = QPixmap(path)
        if pixmap.isNull():
            logger.debug("cannot load thumbnail: %s", path)
            return None

        # 화면이 실제로 찍을 픽셀 수만큼 줄인다 — hidpi_image 참조
        scaled = scaled_for_screen(pixmap, QSize(_THUMB_SIZE, _THUMB_SIZE), ratio)

        # Cache with LRU eviction
        self._cache[key] = scaled
        self._cache_order.append(key)
        if len(self._cache_order) > _MAX_CACHE:
            evict = self._cache_order.pop(0)
            self._cache.pop(evict, None)

        return scaled

    def _draw_broken_placeholder(self, painter, rect) -> None:
        """깨진 이미지 자리 표시."""
        painter.setPen(Qt.GlobalColor.gray)
        painter.drawRect(rect.adjusted(2, 2, -2, -2))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "?")


class GalleryView(QDialog):
    """Scrollable thumbnail grid with metadata inspection.

    `QDialog`인 이유: 부모(메인 윈도우)를 가진 `QWidget`을 `show()`하면 창이 아니라
    **자식 위젯**으로 메인 윈도우 안에 그려진다. 제목표시줄도 닫기 버튼도 없어 닫을
    방법이 없는 흰 사각형이 되어 버렸다 (v0.2.3). 프로젝트의 다른 창들과 같이 QDialog로
    두면 창 장식과 Esc 닫기가 따라온다. 모달은 아니다 — 생성 중에도 계속 떠 있어야 한다.

    Requirements 5.1–5.9 구현:
      - 150×150 스크롤 썸네일 그리드
      - 클릭 → 전체 이미지 + 메타데이터 표시
      - 뷰포트 + 1 스크린 버퍼 레이지 로딩 (QListView의 uniform item sizes + delegate)
      - 생성 중 auto-append
      - date/name 정렬
      - 우클릭 컨텍스트 메뉴: Open in Explorer, Copy Seed, Reuse Settings
      - 빈 디렉토리 → 자리 표시 메시지
      - 읽을 수 없는 PNG → 깨진 이미지 자리 표시 + 메타데이터 스킵
    """

    reuse_requested = Signal(str)  # path to PNG — 이미지 정보 창을 열어 달라는 요청
    settings_reused = Signal(object)  # ReusableSettings — 창 안에서 이미 고른 설정
    send_to_i2i = Signal(str)  # 이 이미지를 i2i 원본으로
    send_to_enhance = Signal(str)  # 이 이미지를 강화 원본으로
    images_deleted = Signal(list)  # 휴지통으로 보낸 경로들

    def __init__(
        self,
        save_dir: str,
        i18n: I18nManager,
        parent: QWidget | None = None,
        *,
        favorites: FavoriteStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._save_dir = save_dir
        self._i18n = i18n
        self._favorites = favorites if favorites is not None else FavoriteStore()
        #: 경로 → 검색용 글자(프롬프트 등). 검색어를 처음 칠 때부터 백그라운드에서 채운다.
        self._search_index: dict[str, str] = {}
        self._index_results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._index_stop = threading.Event()
        self._index_thread: threading.Thread | None = None

        # Model
        self._model = GalleryModel(self)
        self._model.set_favorite_lookup(lambda path: path in self._favorites)

        # Delegate
        self._delegate = _ThumbnailDelegate(self)

        # View
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setFlow(QListView.Flow.LeftToRight)
        self._view.setWrapping(True)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setGridSize(QSize(_THUMB_SIZE + 10, _THUMB_SIZE + 10))
        self._view.setUniformItemSizes(True)
        # Ctrl·Shift로 여러 장을 골라 한꺼번에 지우거나 즐겨찾기에 넣는다
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setSpacing(4)

        # Lazy loading: QListView + uniform item sizes means Qt only requests
        # paint for visible items. The delegate handles loading on paint().
        # We also configure layout mode to batch for smoother scrolling.
        self._view.setLayoutMode(QListView.LayoutMode.Batched)
        self._view.setBatchSize(30)  # items per layout pass

        # Context menu on right-click
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)

        # Click handler for metadata display
        self._view.clicked.connect(self._on_thumbnail_clicked)

        # Empty-directory placeholder label (Requirement 5.8)
        self._placeholder = QLabel(i18n.get_text("gallery.empty"), self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: gray; font-size: 14px;")
        self._placeholder.setVisible(False)

        # 폴더 줄 — 지금 보고 있는 폴더를 밝히고, 그 자리에서 다른 폴더로 옮겨 갈 수 있게 한다.
        # 여기서 고른 폴더는 이 창에만 적용된다 (설정에 남기려면 옵션 → 폴더).
        self._folder_button = QPushButton(i18n.get_text("gallery.select_folder"), self)
        self._folder_button.setToolTip(i18n.get_text("gallery.select_folder_tooltip"))
        self._folder_button.clicked.connect(self._on_choose_folder)
        self._folder_label = QLabel(self)
        self._folder_label.setStyleSheet("color: gray;")
        self._folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(6, 6, 6, 0)
        toolbar.addWidget(self._folder_button)
        toolbar.addWidget(self._folder_label, 1)

        # 검색 줄 — 검색어 / 즐겨찾기만 / 정렬 / 장수
        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText(i18n.get_text("gallery.search_placeholder"))
        self._search_edit.setToolTip(i18n.get_text("gallery.search_tooltip"))
        self._search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_filter)
        self._search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        self._favorites_check = QCheckBox(i18n.get_text("gallery.favorites_only"), self)
        self._favorites_check.toggled.connect(lambda _on: self._apply_filter())
        self._sort_combo = QComboBox(self)
        self._sort_combo.addItem(i18n.get_text("gallery.sort_date_desc"), "date")
        self._sort_combo.addItem(i18n.get_text("gallery.sort_name_asc"), "name")
        self._sort_combo.currentIndexChanged.connect(
            lambda _index: self.set_sort_order(self._sort_combo.currentData())
        )
        self._count_label = QLabel(self)
        self._count_label.setStyleSheet("color: gray;")

        search_row = QHBoxLayout()
        search_row.setContentsMargins(6, 0, 6, 0)
        search_row.addWidget(self._search_edit, 1)
        search_row.addWidget(self._favorites_check)
        search_row.addWidget(self._sort_combo)
        search_row.addWidget(self._count_label)

        # 색인 결과를 GUI 스레드에서 걷어 간다. 워커는 큐에 넣기만 하고 Qt 객체를 만지지
        # 않는다 — 창이 닫힌 뒤 워커가 시그널을 쏘다 죽는 일(세그폴트)이 구조적으로 없다.
        self._index_timer = QTimer(self)
        self._index_timer.setInterval(_INDEX_POLL_MS)
        self._index_timer.timeout.connect(self._drain_index_results)

        # 고른 이미지에 대한 단축키 — 격자에 포커스가 있을 때만 (검색창에서 F를 쳐도 안 걸린다)
        for key, handler in (
            (QKeySequence.StandardKey.Delete, self.delete_selected),
            (QKeySequence("F"), self.toggle_favorite_selected),
        ):
            shortcut = QShortcut(key, self._view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(handler)
        self._model.modelReset.connect(self._update_count)
        self._model.rowsInserted.connect(lambda *_args: self._update_count())
        self._view.selectionModel().selectionChanged.connect(lambda *_args: self._update_count())

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(toolbar)
        layout.addLayout(search_row)
        layout.addWidget(self._view)
        layout.addWidget(self._placeholder)

        # 썸네일을 많이 늘어놓는 창이다 — 최소화·최대화 단추를 붙인다.
        # (`show()` 전에 불러야 플래그가 먹는다.)
        enable_window_controls(self)

        # Initial load
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """폴더를 다시 훑어 격자를 채운다.

        하위 폴더까지 본다 — V5는 결과를 한 폴더에 평평하게 쌓지만, V4 시절 날짜별로
        나눠 둔 폴더를 지정하는 경우가 있다.
        """
        self._folder_label.setText(self._save_dir)
        save_path = Path(self._save_dir)
        if not save_path.is_dir():
            self._model.set_entries([])
            self._update_placeholder()
            return
        image_files = [
            str(p) for p in save_path.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES and p.is_file()
        ]
        self._model.set_entries(image_files)
        self._update_placeholder()
        if self._query():
            self._start_indexing()  # 폴더가 바뀌었다 — 새 파일들의 프롬프트도 읽어야 걸린다

    def set_directory(self, path: str) -> None:
        """볼 폴더를 바꾸고 다시 훑는다."""
        self._save_dir = path
        self.refresh()

    @property
    def directory(self) -> str:
        """지금 보고 있는 폴더."""
        return self._save_dir

    def _on_choose_folder(self) -> None:
        """`폴더 선택` — 이 창에서만 쓸 폴더를 고른다."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            self._i18n.get_text("gallery.folder_dialog_title"),
            self._save_dir,
        )
        if chosen:
            self.set_directory(chosen)

    def append_image(self, path: str) -> None:
        """Append a newly generated image without full refresh.

        Called by the generation service when a new image is saved.
        The model inserts at the correct sorted position.

        보고 있는 폴더 밖의 이미지는 무시한다 — 갤러리를 다른 폴더로 옮겨 둔 채 생성하면
        그 폴더에 있지도 않은 이미지가 섞여 보인다.
        """
        if not self._contains(path):
            return
        if self._query():
            # 검색 중이면 새 이미지도 바로 걸러야 한다 — 한 장이라 그 자리에서 읽는다
            self._search_index[path] = searchable_text(read_metadata(path))
        self._model.append_image(path)
        self._update_placeholder()

    def _contains(self, path: str) -> bool:
        """`path`가 지금 보고 있는 폴더(하위 포함) 안에 있는가."""
        try:
            Path(path).resolve().relative_to(Path(self._save_dir).resolve())
        except (ValueError, OSError):
            return False
        return True

    def set_sort_order(self, order: Literal["date", "name"]) -> None:
        """Change sort order: 'date' (newest first) or 'name' (alpha ascending)."""
        self._model.set_sort_order(order)

    @property
    def sort_order(self) -> Literal["date", "name"]:
        """Current sort order."""
        return self._model.sort_order

    @property
    def model(self) -> GalleryModel:
        """Expose the model for testing."""
        return self._model

    # ------------------------------------------------------------------
    # Click handler — show full image + parsed PNG metadata (Req 5.2)
    # ------------------------------------------------------------------

    def _on_thumbnail_clicked(self, index: QModelIndex) -> None:
        """Open ImageInfoDialog to show full image + parsed metadata."""
        # Ctrl·Shift 클릭은 여러 장을 고르는 중이다 — 그때마다 창이 뜨면 고를 수가 없다
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        path = self._model.path_at(index)
        if not path:
            return

        from pathlib import Path as _Path

        from .image_info_dialog import ImageInfoDialog

        dialog = ImageInfoDialog(self._i18n, parent=self)
        dialog.load_file(_Path(path))

        def _reuse(settings) -> None:
            # 여기서 고른 항목을 그대로 넘긴다 — 메인 창의 이미지 정보 창을 한 번 더
            # 띄우면 (모달인 이 창 뒤에 가려서) 손댈 수도 없고, 고른 것도 버려진다.
            dialog.accept()
            self.settings_reused.emit(settings)

        dialog.settings_selected.connect(_reuse)
        dialog.exec()

    # ------------------------------------------------------------------
    # Right-click context menu (Req 5.6, 5.7)
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        """우클릭 메뉴. 한 장짜리 동작은 누른 그 이미지에, 나머지는 고른 것 전부에 적용한다."""
        index = self._view.indexAt(pos)
        if not index.isValid():
            return

        path = self._model.path_at(index)
        if not path:
            return
        # 고르지 않은 이미지를 우클릭하면 그 한 장만 대상이다 (탐색기와 같은 규칙)
        selected = self.selected_paths()
        targets = selected if path in selected else [path]
        tr = self._i18n.get_text

        menu = QMenu(self)

        def add(text: str, handler: Callable[[], None]) -> None:
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False: handler())
            menu.addAction(action)

        add(tr("gallery.menu_open_folder"), lambda: self._open_in_explorer(path))
        add(tr("gallery.menu_copy_seed"), lambda: self._copy_seed(path))
        add(tr("gallery.copy_prompt"), lambda: self._copy_prompt(path))
        add(tr("gallery.menu_reuse"), lambda: self.reuse_requested.emit(path))
        menu.addSeparator()
        add(tr("gallery.menu_send_i2i"), lambda: self.send_to_i2i.emit(path))
        add(tr("gallery.menu_send_enhance"), lambda: self.send_to_enhance.emit(path))
        menu.addSeparator()
        all_favorite = all(target in self._favorites for target in targets)
        add(
            tr("gallery.menu_favorite_remove") if all_favorite else tr("gallery.menu_favorite_add"),
            lambda: self.set_favorite(targets, not all_favorite),
        )
        add(tr("gallery.menu_delete", len(targets)), lambda: self.delete_paths(targets))

        menu.exec(self._view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # 선택 · 즐겨찾기 · 삭제
    # ------------------------------------------------------------------

    def selected_paths(self) -> list[str]:
        """고른 이미지들의 경로 (화면에 보이는 순서)."""
        rows = sorted(index.row() for index in self._view.selectionModel().selectedIndexes())
        paths = [self._model.path_at(self._model.index(row, 0)) for row in rows]
        return [path for path in paths if path]

    def set_favorite(self, paths: list[str], favorite: bool) -> None:
        self._favorites.set_favorite(paths, favorite)
        if self._favorites_check.isChecked():
            self._model.refilter()  # 즐겨찾기만 보는 중이면 빠진 것은 사라져야 한다
        else:
            self._model.favorites_changed()

    def toggle_favorite_selected(self) -> None:
        """고른 이미지의 즐겨찾기를 뒤집는다 (`F`). 섞여 있으면 전부 켠다."""
        paths = self.selected_paths()
        if paths:
            self.set_favorite(paths, not all(path in self._favorites for path in paths))

    def delete_selected(self) -> int:
        """고른 이미지를 휴지통으로 (`Delete`). 지운 장수를 돌려준다."""
        return self.delete_paths(self.selected_paths())

    def delete_paths(self, paths: list[str]) -> int:
        """확인을 받고 휴지통으로 보낸다. 지운 장수를 돌려준다 (취소하면 0)."""
        if not paths or not self._confirm_delete(len(paths)):
            return 0
        removed: list[str] = []
        failed = 0
        for path in paths:
            try:
                send_to_trash(Path(path))
            except OSError as e:
                logger.warning("cannot delete %s: %s", path, e)
                failed += 1
                continue
            removed.append(path)
        if removed:
            for path in removed:
                self._search_index.pop(path, None)
            self._favorites.set_favorite(removed, False)
            self._model.remove_paths(removed)
            self._update_placeholder()
            self.images_deleted.emit(removed)
        if failed:
            tr = self._i18n.get_text
            QMessageBox.warning(self, tr("errors.title"), tr("gallery.delete_failed", failed))
        return len(removed)

    def _confirm_delete(self, count: int) -> bool:
        tr = self._i18n.get_text
        answer = QMessageBox.question(
            self,
            tr("gallery.delete_title"),
            tr("gallery.delete_confirm", count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # 검색 (파일 이름 + 프롬프트)
    # ------------------------------------------------------------------

    def _query(self) -> str:
        return self._search_edit.text().strip()

    def set_search_text(self, text: str) -> None:
        """검색어를 넣고 **바로** 거른다 (입력 지연 없이 — 코드에서 부를 때 쓴다)."""
        self._search_edit.setText(text)
        self._search_timer.stop()
        self._apply_filter()

    def set_favorites_only(self, on: bool) -> None:
        self._favorites_check.setChecked(on)

    def _apply_filter(self) -> None:
        query = self._query()
        favorites_only = self._favorites_check.isChecked()
        if not query and not favorites_only:
            self._model.set_filter(None)
        else:

            def visible(entry: _ImageEntry) -> bool:
                if favorites_only and entry.path not in self._favorites:
                    return False
                return matches(query, entry.filename, self._search_index.get(entry.path, ""))

            self._model.set_filter(visible)
        self._update_placeholder()
        if query:
            self._start_indexing()

    def _start_indexing(self) -> None:
        """아직 프롬프트를 읽지 않은 파일을 백그라운드에서 읽는다.

        검색어를 처음 칠 때 시작한다 — 갤러리를 여는 것만으로 수천 장을 읽지는 않는다.
        읽은 것은 창이 살아 있는 동안 남으므로 두 번째 검색부터는 바로 걸린다.
        """
        if self._index_thread is not None and self._index_thread.is_alive():
            return
        pending = [path for path in self._model.all_paths() if path not in self._search_index]
        if not pending:
            return
        self._index_stop.clear()
        self._index_thread = threading.Thread(
            target=_index_worker,
            args=(pending, self._index_results, self._index_stop),
            name="naiauto-gallery-index",
            daemon=True,
        )
        self._index_thread.start()
        self._index_timer.start()

    def _drain_index_results(self) -> None:
        """워커가 읽어 둔 것을 색인에 옮기고, 새로 걸리는 것이 있으면 다시 거른다."""
        changed = False
        while True:
            try:
                path, text = self._index_results.get_nowait()
            except queue.Empty:
                break
            self._search_index[path] = text
            changed = changed or bool(text)
        if changed and self._query():
            self._model.refilter()
            self._update_placeholder()
        if self._index_thread is None or not self._index_thread.is_alive():
            if self._index_results.empty():
                self._index_timer.stop()
        self._update_count()

    def finish_indexing(self, timeout: float = 30.0) -> None:
        """색인이 끝날 때까지 기다렸다가 반영한다 (테스트용 — 화면에서는 부르지 않는다)."""
        if self._index_thread is not None:
            self._index_thread.join(timeout)
        self._drain_index_results()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        self._index_stop.set()  # 닫힌 창을 위해 파일을 계속 읽을 이유가 없다
        self._index_timer.stop()
        super().closeEvent(event)

    def _update_count(self) -> None:
        """`보이는 장수 / 전체` — 고른 것이 있으면 그 수도."""
        tr = self._i18n.get_text
        text = tr("gallery.count", self._model.rowCount(), self._model.total_count)
        selected = len(self._view.selectionModel().selectedIndexes())
        if selected > 1:
            text = f"{tr('gallery.selection_count', selected)} · {text}"
        if self._index_thread is not None and self._index_thread.is_alive():
            text = f"{tr('gallery.indexing')} · {text}"
        self._count_label.setText(text)

    def _open_in_explorer(self, path: str) -> None:
        """Open the parent directory in the system file manager."""
        parent_dir = str(Path(path).parent)
        if sys.platform == "win32":
            os.startfile(parent_dir)  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(parent_dir))

    def _copy_seed(self, path: str) -> None:
        """Parse metadata and copy seed to clipboard. Skip if unreadable."""
        metadata = read_metadata(path)
        if not metadata:
            return
        settings = extract_reusable(metadata)
        if settings.seed is not None:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(str(settings.seed))

    def _copy_prompt(self, path: str) -> None:
        """프롬프트를 클립보드로. 메타데이터가 없으면 아무것도 하지 않는다."""
        settings = extract_reusable(read_metadata(path))
        if settings.prompt:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(settings.prompt)

    # ------------------------------------------------------------------
    # Empty directory placeholder (Req 5.8)
    # ------------------------------------------------------------------

    def _update_placeholder(self) -> None:
        """Show/hide the placeholder based on whether the model is empty."""
        is_empty = self._model.rowCount() == 0
        filtered = is_empty and self._model.total_count > 0  # 있긴 한데 검색에 안 걸린 것
        self._placeholder.setText(self._i18n.get_text("gallery.no_match" if filtered else "gallery.empty"))
        self._placeholder.setVisible(is_empty)
        self._view.setVisible(not is_empty)
        self._update_count()


def _index_worker(paths: list[str], results: queue.Queue, stop: threading.Event) -> None:
    """파일마다 메타데이터를 읽어 검색용 글자를 큐에 넣는다 (워커 스레드).

    Qt 객체는 하나도 만지지 않는다. 읽지 못한 파일도 빈 글자로 넣는다 — 그래야 다음
    검색 때 같은 파일을 또 읽으려 들지 않는다.
    """
    for path in paths:
        if stop.is_set():
            return
        try:
            text = searchable_text(read_metadata(path))
        except Exception:  # 깨진 파일 하나가 색인 전체를 멈추면 안 된다
            logger.debug("cannot index %s", path, exc_info=True)
            text = ""
        results.put((path, text))
