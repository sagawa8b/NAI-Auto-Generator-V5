"""이미지 정보 다이얼로그 — 드래그 앤 드롭, 스플리터, 필드별 체크박스 지원.

모드리스(비차단) 대화상자로, 파일 없이 열 수 있고 드래그 앤 드롭 또는
파일 열기 버튼으로 이미지를 불러온다. 왼쪽 패널에 메타데이터 폼,
오른쪽에 확대/축소 이미지 미리보기를 수평 스플리터로 배치한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.metadata.naiinfo import read_metadata
from ..core.metadata.reuse import ReusableSettings, extract_reusable
from .widgets.dialog_image_view import DialogImageView

_VALID_SUFFIXES = {".png", ".webp"}
_SETTINGS_GEOMETRY_KEY = "image_info/geometry"
_SETTINGS_SPLITTER_KEY = "image_info/splitter"


class ImageInfoDialog(QDialog):
    """생성 정보를 보여주고, Apply 클릭 시 ReusableSettings를 시그널로 내보낸다.

    모드리스 대화상자로, 파일 없이 열 수 있다. 드래그 앤 드롭 또는
    파일 열기 버튼으로 이미지를 불러온다.
    """

    settings_selected = Signal(object)  # ReusableSettings

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._qsettings = QSettings()
        self._first_show = True
        self._current_settings: ReusableSettings | None = None
        self._checkboxes: dict[str, QCheckBox] = {}

        tr = i18n.get_text
        self.setWindowTitle(tr("image_info.title"))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAcceptDrops(True)
        self.setMinimumSize(700, 500)

        main_layout = QVBoxLayout(self)

        # ── Top bar: file-open button + filename label ──
        top_bar = QHBoxLayout()
        self._open_button = QPushButton(tr("image_info.open_file"))
        self._open_button.clicked.connect(self._on_open_file)
        top_bar.addWidget(self._open_button)

        self._filename_label = QLabel(tr("image_info.no_file"))
        self._filename_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top_bar.addWidget(self._filename_label, stretch=1)
        main_layout.addLayout(top_bar)

        # ── Horizontal splitter: left panel + right panel ──
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel: scroll area with form
        self._left_scroll = QScrollArea()
        self._left_scroll.setWidgetResizable(True)
        self._left_widget = QWidget()
        self._left_layout = QVBoxLayout(self._left_widget)
        self._left_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Notice label (shown when no file or no metadata)
        self._notice_label = QLabel(tr("image_info.drop_hint"))
        self._notice_label.setWordWrap(True)
        self._notice_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._left_layout.addWidget(self._notice_label)

        # Field checkboxes container
        self._fields_widget = QWidget()
        self._fields_layout = QVBoxLayout(self._fields_widget)
        self._fields_layout.setContentsMargins(0, 0, 0, 0)
        self._create_field_checkboxes()
        self._left_layout.addWidget(self._fields_widget)
        self._fields_widget.setVisible(False)

        # Raw JSON view
        raw_label = QLabel(tr("image_info.field_raw"))
        self._left_layout.addWidget(raw_label)
        self._raw_view = QPlainTextEdit()
        self._raw_view.setReadOnly(True)
        self._raw_view.setMaximumHeight(200)
        self._left_layout.addWidget(self._raw_view)

        self._left_scroll.setWidget(self._left_widget)
        self._splitter.addWidget(self._left_scroll)

        # Right panel: DialogImageView
        self._image_view = DialogImageView()
        self._splitter.addWidget(self._image_view)

        # Default splitter ratio ~40/60
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 3)

        main_layout.addWidget(self._splitter, stretch=1)

        # ── Zoom hint label ──
        zoom_hint = QLabel(tr("image_info.zoom_hint"))
        zoom_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        main_layout.addWidget(zoom_hint)

        # ── Bottom buttons: Apply Selected + Close ──
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()
        self._apply_button = QPushButton(tr("image_info.apply_selected"))
        self._apply_button.clicked.connect(self._on_apply)
        self._apply_button.setEnabled(False)
        bottom_bar.addWidget(self._apply_button)

        self._close_button = QPushButton(tr("image_info.close"))
        self._close_button.clicked.connect(self.close)
        bottom_bar.addWidget(self._close_button)
        main_layout.addLayout(bottom_bar)

    # ── Field checkboxes ─────────────────────────────────────────────

    def _create_field_checkboxes(self) -> None:
        tr = self._i18n.get_text
        fields = [
            ("prompt", "image_info.field_prompt"),
            ("negative_prompt", "image_info.field_negative"),
            ("seed", "image_info.field_seed"),
            ("steps", "image_info.field_steps"),
            ("scale", "image_info.field_scale"),
            ("sampler", "image_info.field_sampler"),
            ("characters", "image_info.field_characters"),
        ]
        for field_name, i18n_key in fields:
            cb = QCheckBox(tr(i18n_key))
            cb.setChecked(True)
            self._checkboxes[field_name] = cb
            self._fields_layout.addWidget(cb)

    # ── Public API ───────────────────────────────────────────────────

    def load_file(self, path: Path) -> None:
        """파일을 불러와 메타데이터와 이미지를 표시한다."""
        path = Path(path)
        self._filename_label.setText(path.name)
        self._filename_label.setToolTip(str(path))

        metadata = read_metadata(path)
        settings = extract_reusable(metadata)
        self._current_settings = settings

        # Load image into right panel
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self._image_view.setPixmap(pixmap)
        else:
            self._image_view.setText("")

        if settings.is_empty:
            tr = self._i18n.get_text
            self._notice_label.setText(tr("image_info.no_metadata"))
            self._notice_label.setVisible(True)
            self._fields_widget.setVisible(False)
            self._apply_button.setEnabled(False)
            self._raw_view.setPlainText(self._format_raw(metadata))
        else:
            self._notice_label.setVisible(False)
            self._fields_widget.setVisible(True)
            self._apply_button.setEnabled(True)
            self._populate_fields(settings, metadata)
            self._raw_view.setPlainText(self._format_raw(metadata))

    # ── Drag and Drop ────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if file_path and Path(file_path).suffix.lower() in _VALID_SUFFIXES:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if file_path and Path(file_path).suffix.lower() in _VALID_SUFFIXES:
                    self.load_file(Path(file_path))
                    event.acceptProposedAction()
                    return
        event.ignore()

    # ── File open button ─────────────────────────────────────────────

    def _on_open_file(self) -> None:
        tr = self._i18n.get_text
        filter_str = f"{tr('image_info.filter_images')} (*.png *.webp)"
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("image_info.open_file"),
            "",
            filter_str,
        )
        if file_path:
            self.load_file(Path(file_path))

    # ── Apply button ─────────────────────────────────────────────────

    def _on_apply(self) -> None:
        if self._current_settings and not self._current_settings.is_empty:
            self.settings_selected.emit(self._current_settings)

    # ── Geometry persistence ─────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        self._qsettings.setValue(_SETTINGS_GEOMETRY_KEY, self.saveGeometry())
        self._qsettings.setValue(_SETTINGS_SPLITTER_KEY, self._splitter.saveState())
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            geometry = self._qsettings.value(_SETTINGS_GEOMETRY_KEY)
            if geometry:
                self.restoreGeometry(geometry)
            splitter_state = self._qsettings.value(_SETTINGS_SPLITTER_KEY)
            if splitter_state:
                self._splitter.restoreState(splitter_state)

    # ── Internal helpers ─────────────────────────────────────────────

    def _populate_fields(self, settings: ReusableSettings, metadata: dict | None) -> None:
        """체크박스 텍스트에 값을 반영한다."""
        tr = self._i18n.get_text

        def _set_field(key: str, i18n_key: str, value: object) -> None:
            cb = self._checkboxes[key]
            if value is not None and value != "":
                text = f"{tr(i18n_key)}: {value}"
            else:
                text = f"{tr(i18n_key)}: —"
            cb.setText(text)
            cb.setChecked(True)

        _set_field("prompt", "image_info.field_prompt", _truncate(settings.prompt, 80))
        _set_field("negative_prompt", "image_info.field_negative", _truncate(settings.negative_prompt, 80))
        _set_field("seed", "image_info.field_seed", settings.seed)
        _set_field("steps", "image_info.field_steps", settings.steps)
        _set_field("scale", "image_info.field_scale", settings.cfg_scale)
        _set_field("sampler", "image_info.field_sampler", settings.sampler)

        # Characters summary
        if settings.characters:
            summary = ", ".join(c.prompt[:30] for c in settings.characters)
            _set_field("characters", "image_info.field_characters", summary)
        else:
            _set_field("characters", "image_info.field_characters", None)

    def _format_raw(self, metadata: dict | None) -> str:
        if not metadata:
            return self._i18n.get_text("image_info.no_metadata")
        comment = metadata.get("comment")
        if isinstance(comment, dict):
            return json.dumps(comment, indent=2, ensure_ascii=False)
        return json.dumps(metadata.get("raw", {}), indent=2, ensure_ascii=False)


def _truncate(text: str | None, max_len: int) -> str | None:
    """긴 텍스트를 요약용으로 잘라낸다."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"
