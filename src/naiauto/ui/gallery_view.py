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

구현:
  - QListView + QAbstractListModel + QStyledItemDelegate 패턴
  - 썸네일은 뷰포트 + 1 화면 버퍼 내에서만 QPixmap으로 로드
  - LRU 캐시로 메모리 제한
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListView,
    QMenu,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.metadata.naiinfo import read_metadata
from ..core.metadata.reuse import extract_reusable

logger = logging.getLogger(__name__)

_THUMB_SIZE = 150
_MAX_CACHE = 200


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
    """갤러리 이미지 목록 모델 — 경로 기반, 정렬 내장."""

    PathRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[_ImageEntry] = []
        self._sort_order: Literal["date", "name"] = "date"

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
        return None

    # --- Public API ---

    def set_entries(self, paths: list[str]) -> None:
        """전체 목록 교체 (refresh 용)."""
        self.beginResetModel()
        self._entries = [_ImageEntry(p) for p in paths]
        self._apply_sort()
        self.endResetModel()

    def append_image(self, path: str) -> None:
        """새 이미지 추가. 정렬 기준에 따라 적절한 위치에 삽입."""
        entry = _ImageEntry(path)
        pos = self._insert_position(entry)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._entries.insert(pos, entry)
        self.endInsertRows()

    def set_sort_order(self, order: Literal["date", "name"]) -> None:
        """정렬 기준 변경 후 전체 재정렬."""
        if order == self._sort_order:
            return
        self._sort_order = order
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    @property
    def sort_order(self) -> Literal["date", "name"]:
        return self._sort_order

    def path_at(self, index: QModelIndex) -> str | None:
        """주어진 인덱스의 파일 경로 반환."""
        if not index.isValid() or index.row() >= len(self._entries):
            return None
        return self._entries[index.row()].path

    # --- Internal ---

    def _apply_sort(self) -> None:
        if self._sort_order == "date":
            self._entries.sort(key=lambda e: e.mtime, reverse=True)
        else:
            self._entries.sort(key=lambda e: e.filename.lower())

    def _insert_position(self, entry: _ImageEntry) -> int:
        """이진 탐색으로 정렬 유지하는 삽입 위치 결정."""
        lo, hi = 0, len(self._entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._comes_before(entry, self._entries[mid]):
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
        self._cache: dict[str, QPixmap] = {}
        self._cache_order: list[str] = []
        self._broken_placeholder: QPixmap | None = None

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(_THUMB_SIZE, _THUMB_SIZE)

    def paint(self, painter, option, index) -> None:
        painter.save()
        # Background
        if option.state & option.State_Selected:  # type: ignore[attr-defined]
            painter.fillRect(option.rect, option.palette.highlight())

        path = index.data(GalleryModel.PathRole)
        if path:
            pixmap = self._get_thumbnail(path)
            if pixmap and not pixmap.isNull():
                # Center the thumbnail in the cell
                x = option.rect.x() + (option.rect.width() - pixmap.width()) // 2
                y = option.rect.y() + (option.rect.height() - pixmap.height()) // 2
                painter.drawPixmap(x, y, pixmap)
            else:
                # Broken image placeholder
                self._draw_broken_placeholder(painter, option.rect)
        painter.restore()

    def _get_thumbnail(self, path: str) -> QPixmap | None:
        """캐시에서 썸네일을 가져오거나, 없으면 로드."""
        if path in self._cache:
            # Move to end (most recently used)
            self._cache_order.remove(path)
            self._cache_order.append(path)
            return self._cache[path]

        # Load and scale
        pixmap = QPixmap(path)
        if pixmap.isNull():
            logger.debug("cannot load thumbnail: %s", path)
            return None

        scaled = pixmap.scaled(
            _THUMB_SIZE,
            _THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        # Cache with LRU eviction
        self._cache[path] = scaled
        self._cache_order.append(path)
        if len(self._cache_order) > _MAX_CACHE:
            evict = self._cache_order.pop(0)
            self._cache.pop(evict, None)

        return scaled

    def _draw_broken_placeholder(self, painter, rect) -> None:
        """깨진 이미지 자리 표시."""
        painter.setPen(Qt.GlobalColor.gray)
        painter.drawRect(rect.adjusted(2, 2, -2, -2))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "?")


class GalleryView(QWidget):
    """Scrollable thumbnail grid with metadata inspection.

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

    reuse_requested = Signal(str)  # path to PNG

    def __init__(self, save_dir: str, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._save_dir = save_dir
        self._i18n = i18n

        # Model
        self._model = GalleryModel(self)

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
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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
        self._placeholder = QLabel("No images available", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: gray; font-size: 14px;")
        self._placeholder.setVisible(False)

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        layout.addWidget(self._placeholder)

        # Initial load
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-scan save_dir for *.png files and reload the grid."""
        save_path = Path(self._save_dir)
        if not save_path.exists():
            self._model.set_entries([])
            self._update_placeholder()
            return
        png_files = [str(p) for p in save_path.iterdir() if p.suffix.lower() == ".png" and p.is_file()]
        self._model.set_entries(png_files)
        self._update_placeholder()

    def append_image(self, path: str) -> None:
        """Append a newly generated image without full refresh.

        Called by the generation service when a new image is saved.
        The model inserts at the correct sorted position.
        """
        self._model.append_image(path)
        self._update_placeholder()

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
        path = self._model.path_at(index)
        if not path:
            return

        from .image_info_dialog import ImageInfoDialog

        dialog = ImageInfoDialog(self._i18n, path, parent=self)
        if dialog.exec() == ImageInfoDialog.DialogCode.Accepted:
            # User chose "Load these settings"
            self.reuse_requested.emit(path)

    # ------------------------------------------------------------------
    # Right-click context menu (Req 5.6, 5.7)
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        """Show context menu with Open in Explorer, Copy Seed, Reuse Settings."""
        index = self._view.indexAt(pos)
        if not index.isValid():
            return

        path = self._model.path_at(index)
        if not path:
            return

        menu = QMenu(self)

        # "Open in Explorer" action
        open_action = QAction("Open in Explorer", self)
        open_action.triggered.connect(lambda: self._open_in_explorer(path))
        menu.addAction(open_action)

        # "Copy Seed" action
        copy_seed_action = QAction("Copy Seed", self)
        copy_seed_action.triggered.connect(lambda: self._copy_seed(path))
        menu.addAction(copy_seed_action)

        # "Reuse Settings" action
        reuse_action = QAction("Reuse Settings", self)
        reuse_action.triggered.connect(lambda: self.reuse_requested.emit(path))
        menu.addAction(reuse_action)

        menu.exec(self._view.viewport().mapToGlobal(pos))

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

    # ------------------------------------------------------------------
    # Empty directory placeholder (Req 5.8)
    # ------------------------------------------------------------------

    def _update_placeholder(self) -> None:
        """Show/hide the placeholder based on whether the model is empty."""
        is_empty = self._model.rowCount() == 0
        self._placeholder.setVisible(is_empty)
        self._view.setVisible(not is_empty)
