"""WD14 태거 다이얼로그 — 이미지 드롭/선택 → 자동 태그 예측 → 프롬프트에 추가."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.wd14_tagger import runtime_error
from .widgets.hidpi_image import HiDpiImageLabel

if TYPE_CHECKING:
    from ..core.i18n.manager import I18nManager
    from ..core.wd14_tagger import WD14Tagger

logger = logging.getLogger(__name__)

_INFERENCE_TIMEOUT_SECONDS = 30
_PREVIEW_SIZE = 200  # 미리보기 한 변 (논리 픽셀)


class _InferenceWorker(QThread):
    """QThread wrapper for WD14 inference with timeout support."""

    finished = Signal(list)  # list[TagPrediction]
    error = Signal(str)

    def __init__(
        self,
        tagger: WD14Tagger,
        image: Image.Image,
        threshold: float = 0.35,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tagger = tagger
        self._image = image
        self._threshold = threshold
        self._timed_out = False
        self._timer: threading.Timer | None = None

    def run(self) -> None:
        """Run inference with a 30-second timeout via threading.Timer."""
        self._timer = threading.Timer(_INFERENCE_TIMEOUT_SECONDS, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

        try:
            predictions = self._tagger.predict(self._image, threshold=self._threshold)
            if self._timed_out:
                return  # timeout already fired error signal
            self._timer.cancel()
            self.finished.emit(predictions)
        except Exception as exc:
            if self._timed_out:
                return
            self._timer.cancel()
            self.error.emit(str(exc))

    def _on_timeout(self) -> None:
        """Called by threading.Timer if inference exceeds 30s."""
        self._timed_out = True
        self.error.emit("WD14 inference timed out (exceeded 30 seconds).")


class WD14Dialog(QDialog):
    """WD14 auto-tagging dialog.

    Shows a drop zone / file selection button for images. On image input,
    runs WD14Tagger.predict() in a QThread. Displays predicted tags in a
    checkable list with confidence scores. User selects which tags to append.
    "Apply" button appends selected tags to prompt (comma-separated).

    Parameters
    ----------
    tagger : WD14Tagger
        The core WD14 tagger instance.
    i18n : I18nManager
        Internationalization manager.
    parent : QWidget | None
        Parent widget.
    """

    tags_selected = Signal(list)  # list[str] — selected tag names

    def __init__(
        self,
        tagger: WD14Tagger,
        i18n: I18nManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tagger = tagger
        self._i18n = i18n
        self._worker: _InferenceWorker | None = None

        tr = i18n.get_text
        self.setWindowTitle(tr("wd14.title"))
        self.setMinimumSize(500, 520)
        self.setAcceptDrops(True)

        self._build_ui()
        self._update_availability()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        tr = self._i18n.get_text
        layout = QVBoxLayout(self)

        # Drop zone / image preview area
        self._image_label = HiDpiImageLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumHeight(120)
        self._image_label.setStyleSheet(
            "QLabel { border: 2px dashed #888; border-radius: 8px; padding: 16px; }"
        )
        self._image_label.setText(tr("wd14.drop_here"))
        layout.addWidget(self._image_label)

        # Browse button
        browse_layout = QHBoxLayout()
        self._browse_btn = QPushButton(tr("wd14.browse"))
        self._browse_btn.clicked.connect(self._on_browse)
        browse_layout.addStretch()
        browse_layout.addWidget(self._browse_btn)
        browse_layout.addStretch()
        layout.addLayout(browse_layout)

        # Progress bar (hidden until inference runs)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Status label
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_label)

        # Tag list (checkable)
        self._tag_list = QListWidget()
        self._tag_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(self._tag_list, stretch=1)

        # Select All / Deselect All
        select_layout = QHBoxLayout()
        self._select_all_btn = QPushButton(tr("wd14.select_all"))
        self._select_all_btn.clicked.connect(self._select_all)
        self._deselect_all_btn = QPushButton(tr("wd14.deselect_all"))
        self._deselect_all_btn.clicked.connect(self._deselect_all)
        self._copy_btn = QPushButton(tr("wd14.copy"))
        self._copy_btn.setToolTip(tr("wd14.copy_tooltip"))
        self._copy_btn.clicked.connect(self._on_copy)
        self._copy_btn.setEnabled(False)  # 결과가 나오기 전에는 복사할 것이 없다
        select_layout.addWidget(self._select_all_btn)
        select_layout.addWidget(self._deselect_all_btn)
        select_layout.addStretch()
        select_layout.addWidget(self._copy_btn)
        layout.addLayout(select_layout)

        # Dialog buttons
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._apply_btn = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        self._apply_btn.setText(tr("wd14.apply"))
        self._apply_btn.setEnabled(False)
        self._button_box.accepted.connect(self._on_apply)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

    # ------------------------------------------------------------------
    # Availability Check
    # ------------------------------------------------------------------

    def _update_availability(self) -> None:
        """Disable the dialog controls if the WD14 model is not installed."""
        if self._tagger.is_available:
            return

        # 무엇이 없어서 못 쓰는지 그대로 알려 준다 — "모델을 내려받으세요"만 띄우면
        # 모델을 이미 받아 둔 사용자는 고칠 방법을 찾을 수 없다.
        tr = self._i18n.get_text
        if not self._tagger.has_model:
            message = tr("wd14.no_model", str(self._tagger.model_path))
            hint = tr("wd14.no_model_hint")
        else:
            message = tr("wd14.no_runtime")
            hint = tr("wd14.no_runtime_hint", runtime_error())
        self._browse_btn.setEnabled(False)
        self._browse_btn.setToolTip(hint)
        self._image_label.setText(message)
        self._image_label.setToolTip(hint)
        self._status_label.setText(hint)
        self._status_label.setWordWrap(True)
        self._apply_btn.setEnabled(False)
        self._select_all_btn.setEnabled(False)
        self._deselect_all_btn.setEnabled(False)
        self._copy_btn.setEnabled(False)
        self.setAcceptDrops(False)

    # ------------------------------------------------------------------
    # Image Input
    # ------------------------------------------------------------------

    def _on_browse(self) -> None:
        """Open file dialog to select an image."""
        tr = self._i18n.get_text
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("wd14.choose_image"),
            "",
            tr("wd14.image_filter"),
        )
        if path:
            self._load_image(Path(path))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept drag if it contains image URLs."""
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    suffix = Path(url.toLocalFile()).suffix.lower()
                    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        event.acceptProposedAction()
                        return

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Handle image drop."""
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = Path(url.toLocalFile())
                    suffix = path.suffix.lower()
                    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        self._load_image(path)
                        event.acceptProposedAction()
                        return

    def _load_image(self, path: Path) -> None:
        """Load image, show preview, and start inference."""
        try:
            image = Image.open(path)
            image.load()  # ensure file is read completely
        except Exception as exc:
            QMessageBox.warning(
                self, self._i18n.get_text("errors.title"), f"{self._i18n.get_text('wd14.open_failed')}\n{exc}"
            )
            return

        # Show preview
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self._image_label.show_fitted(pixmap, QSize(_PREVIEW_SIZE, _PREVIEW_SIZE))
        else:
            self._image_label.setText(path.name)

        # Clear previous results
        self._tag_list.clear()
        self._apply_btn.setEnabled(False)
        self._status_label.setText("")

        # Start inference
        self._run_inference(image)

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def _run_inference(self, image: Image.Image) -> None:
        """Run WD14 inference in a background QThread with timeout."""
        # Disable input during inference
        self._browse_btn.setEnabled(False)
        self.setAcceptDrops(False)
        self._progress.setVisible(True)
        self._status_label.setText(self._i18n.get_text("wd14.running"))

        self._worker = _InferenceWorker(self._tagger, image, parent=self)
        self._worker.finished.connect(self._on_inference_done)
        self._worker.error.connect(self._on_inference_error)
        self._worker.start()

    def _on_inference_done(self, predictions: list) -> None:
        """Handle successful inference results."""
        self._progress.setVisible(False)
        self._browse_btn.setEnabled(True)
        self.setAcceptDrops(True)
        self._worker = None

        if not predictions:
            self._status_label.setText(self._i18n.get_text("wd14.no_tags"))
            self._copy_btn.setEnabled(False)
            return

        self._status_label.setText(self._i18n.get_text("wd14.tags_predicted", len(predictions)))
        self._populate_tag_list(predictions)
        self._apply_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)

    def _on_inference_error(self, message: str) -> None:
        """Handle inference error (including timeout)."""
        self._progress.setVisible(False)
        self._browse_btn.setEnabled(True)
        self.setAcceptDrops(True)
        self._worker = None

        self._status_label.setText(self._i18n.get_text("wd14.error", message))
        logger.error("WD14 inference error: %s", message)

    # ------------------------------------------------------------------
    # Tag List
    # ------------------------------------------------------------------

    def _populate_tag_list(self, predictions: list) -> None:
        """Populate the tag list with checkable items showing confidence."""
        self._tag_list.clear()
        for pred in predictions:
            # Format: "tag_name (0.85)"
            text = f"{pred.tag} ({pred.confidence:.2f})"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, pred.tag)
            self._tag_list.addItem(item)

    def _select_all(self) -> None:
        """Check all items in the tag list."""
        for i in range(self._tag_list.count()):
            self._tag_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _deselect_all(self) -> None:
        """Uncheck all items in the tag list."""
        for i in range(self._tag_list.count()):
            self._tag_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def selected_tags(self) -> list[str]:
        """체크된 태그 이름 — 프롬프트에 넣기와 클립보드 복사가 같은 목록을 쓴다."""
        tags: list[str] = []
        for i in range(self._tag_list.count()):
            item = self._tag_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                tag = item.data(Qt.ItemDataRole.UserRole)
                if tag:
                    tags.append(tag)
        return tags

    def _on_copy(self) -> None:
        """체크한 태그를 프롬프트에 넣을 때와 같은 형태로 클립보드에 복사한다.

        창을 닫지 않는다 — 복사한 뒤 프롬프트에도 넣거나, 선택을 바꿔 다시 복사할 수 있다.
        """
        tr = self._i18n.get_text
        tags = self.selected_tags()
        if not tags:
            self._status_label.setText(tr("wd14.copy_nothing"))
            return

        QGuiApplication.clipboard().setText(", ".join(tags))
        self._status_label.setText(tr("wd14.copied", len(tags)))

    def _on_apply(self) -> None:
        """Collect selected tags and emit signal."""
        selected = self.selected_tags()

        if selected:
            self.tags_selected.emit(selected)

        self.accept()
