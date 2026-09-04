"""이미지 정보 다이얼로그 — 드래그 앤 드롭, 스플리터, 필드별 체크박스 지원.

모드리스(비차단) 대화상자로, 파일 없이 열 수 있고 드래그 앤 드롭 또는
파일 열기 버튼으로 이미지를 불러온다. 왼쪽 패널에 메타데이터 폼,
오른쪽에 확대/축소 이미지 미리보기를 수평 스플리터로 배치한다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QPixmap
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

# 체크박스 하나가 담당하는 ReusableSettings 필드들. 해제하면 그 필드는 None이 되어
# 메인 창의 현재 값이 그대로 남는다 (apply_reusable의 None = 유지 규칙).
_FIELD_TARGETS: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt",),
    "negative_prompt": ("negative_prompt",),
    "seed": ("seed",),
    "steps": ("steps",),
    # Prompt Guidance와 Rescale은 한 쌍으로 움직인다 (한쪽만 되살리면 그림이 달라진다)
    "scale": ("cfg_scale", "cfg_rescale"),
    # 샘플러와 노이즈 스케줄도 마찬가지 — NovelAI는 둘을 함께 고른다
    "sampler": ("sampler", "scheduler"),
    "size": ("width", "height"),
    "characters": ("characters",),
}

# 긴 값은 체크박스 밑에 읽기 전용 상자로 따로 보여 준다 (V4의 이미지 정보 확인기와 같다)
_TEXT_FIELDS = ("prompt", "negative_prompt", "characters")


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
        self._current_path: Path | None = None
        self._checkboxes: dict[str, QCheckBox] = {}
        self._value_views: dict[str, QPlainTextEdit] = {}
        self._rows: dict[str, QWidget] = {}

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

        # 모델·크기처럼 적용 대상이 아닌 정보는 한 줄 요약으로만 보여 준다
        self._summary_label = QLabel("")
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._summary_label.setVisible(False)
        self._left_layout.addWidget(self._summary_label)

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

        # ── Bottom buttons: Copy + Apply Selected + Close ──
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()
        self._copy_button = QPushButton(tr("image_info.copy_selected"))
        self._copy_button.clicked.connect(self._on_copy)
        self._copy_button.setEnabled(False)
        bottom_bar.addWidget(self._copy_button)

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
            ("characters", "image_info.field_characters"),
            ("seed", "image_info.field_seed"),
            ("steps", "image_info.field_steps"),
            ("scale", "image_info.field_scale"),
            ("sampler", "image_info.field_sampler"),
            ("size", "image_info.field_size"),
        ]
        for field_name, i18n_key in fields:
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 4)
            row_layout.setSpacing(2)

            cb = QCheckBox(tr(i18n_key))
            cb.setChecked(True)
            self._checkboxes[field_name] = cb
            row_layout.addWidget(cb)

            if field_name in _TEXT_FIELDS:
                # 프롬프트는 잘라서 보여 주면 쓸모가 없다 — 전문을 그대로 둔다
                view = QPlainTextEdit()
                view.setReadOnly(True)
                view.setMaximumHeight(110)
                self._value_views[field_name] = view
                row_layout.addWidget(view)

            self._rows[field_name] = row
            self._fields_layout.addWidget(row)

    # ── Public API ───────────────────────────────────────────────────

    def load_file(self, path: Path) -> None:
        """파일을 불러와 메타데이터와 이미지를 표시한다."""
        path = Path(path)
        self._current_path = path
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

        self._raw_view.setPlainText(self._format_raw(metadata))
        self._summary_label.setText(self._format_summary(metadata, settings))
        self._summary_label.setVisible(bool(self._summary_label.text()))

        if settings.is_empty:
            tr = self._i18n.get_text
            self._notice_label.setText(tr("image_info.no_metadata"))
            self._notice_label.setVisible(True)
            self._fields_widget.setVisible(False)
            self._apply_button.setEnabled(False)
            self._copy_button.setEnabled(False)
        else:
            self._notice_label.setVisible(False)
            self._fields_widget.setVisible(True)
            self._apply_button.setEnabled(True)
            self._copy_button.setEnabled(True)
            self._populate_fields(settings)

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
        start_dir = str(self._current_path.parent) if self._current_path else ""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("image_info.open_file"),
            start_dir,
            filter_str,
        )
        if file_path:
            self.load_file(Path(file_path))

    # ── Apply / Copy buttons ─────────────────────────────────────────

    def selected_settings(self) -> ReusableSettings:
        """체크된 항목만 남긴 설정. 해제한 필드는 None(=메인 창의 현재 값 유지)."""
        settings = self._current_settings or ReusableSettings()
        cleared: dict[str, object] = {}
        for field_name, targets in _FIELD_TARGETS.items():
            checkbox = self._checkboxes.get(field_name)
            if checkbox is not None and checkbox.isChecked():
                continue
            for target in targets:
                cleared[target] = () if target == "characters" else None
        return replace(settings, **cleared) if cleared else settings

    def _on_apply(self) -> None:
        if self._current_settings and not self._current_settings.is_empty:
            self.settings_selected.emit(self.selected_settings())

    def _on_copy(self) -> None:
        text = self._format_selection(self.selected_settings())
        if text:
            QGuiApplication.clipboard().setText(text)

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

    def _populate_fields(self, settings: ReusableSettings) -> None:
        """체크박스 라벨과 값 상자를 채운다. 값이 없는 줄은 감춘다."""
        tr = self._i18n.get_text

        def _row(key: str, i18n_key: str, value: object, body: str | None = None) -> None:
            row = self._rows[key]
            present = value is not None and value != "" and value != ()
            row.setVisible(present)
            checkbox = self._checkboxes[key]
            checkbox.setChecked(present)
            checkbox.setEnabled(present)
            if body is None:
                checkbox.setText(f"{tr(i18n_key)}: {value}" if present else tr(i18n_key))
            else:
                checkbox.setText(tr(i18n_key))
                self._value_views[key].setPlainText(body if present else "")

        _row("prompt", "image_info.field_prompt", settings.prompt, settings.prompt or "")
        _row(
            "negative_prompt",
            "image_info.field_negative",
            settings.negative_prompt,
            settings.negative_prompt or "",
        )
        _row(
            "characters",
            "image_info.field_characters",
            settings.characters,
            _format_characters(settings),
        )
        _row("seed", "image_info.field_seed", settings.seed)
        _row("steps", "image_info.field_steps", settings.steps)
        _row("scale", "image_info.field_scale", settings.cfg_scale)
        _row("sampler", "image_info.field_sampler", settings.sampler)
        size = f"{settings.width}x{settings.height}" if settings.width and settings.height else None
        _row("size", "image_info.field_size", size)

    def _format_summary(self, metadata: dict | None, settings: ReusableSettings) -> str:
        tr = self._i18n.get_text
        parts: list[str] = []
        model = (metadata or {}).get("model")
        if isinstance(model, str) and model:
            parts.append(f"{tr('image_info.field_model')}: {model}")
        if settings.width and settings.height:
            parts.append(f"{tr('image_info.field_size')}: {settings.width}x{settings.height}")
        return "   ".join(parts)

    def _format_selection(self, settings: ReusableSettings) -> str:
        tr = self._i18n.get_text
        blocks: list[str] = []
        for i18n_key, value in (
            ("image_info.field_prompt", settings.prompt),
            ("image_info.field_negative", settings.negative_prompt),
            ("image_info.field_characters", _format_characters(settings) or None),
        ):
            if value:
                blocks.append(f"--- {tr(i18n_key)} ---\n{value}")
        inline = [
            f"{tr(i18n_key)}: {value}"
            for i18n_key, value in (
                ("image_info.field_seed", settings.seed),
                ("image_info.field_steps", settings.steps),
                ("image_info.field_scale", settings.cfg_scale),
                ("image_info.field_sampler", settings.sampler),
                (
                    "image_info.field_size",
                    f"{settings.width}x{settings.height}" if settings.width and settings.height else None,
                ),
            )
            if value is not None
        ]
        if inline:
            blocks.append("\n".join(inline))
        return "\n\n".join(blocks)

    def _format_raw(self, metadata: dict | None) -> str:
        if not metadata:
            return self._i18n.get_text("image_info.no_metadata")
        comment = metadata.get("comment")
        if isinstance(comment, dict):
            return json.dumps(comment, indent=2, ensure_ascii=False)
        return json.dumps(metadata.get("raw", {}), indent=2, ensure_ascii=False)


def _format_characters(settings: ReusableSettings) -> str:
    """캐릭터 프롬프트를 한 줄에 하나씩 (네거티브가 있으면 함께) 보여 준다."""
    lines: list[str] = []
    for index, caption in enumerate(settings.characters, start=1):
        lines.append(f"{index}. {caption.prompt}")
        if caption.uc:
            lines.append(f"   - {caption.uc}")
    return "\n".join(lines)
