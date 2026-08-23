"""이미지 정보 다이얼로그 — PNG의 NovelAI 생성 정보 표시 + 설정 재사용."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.metadata.naiinfo import read_metadata
from ..core.metadata.reuse import ReusableSettings, extract_reusable


class ImageInfoDialog(QDialog):
    """생성 정보를 보여주고, 수락 시 settings 속성으로 재사용 값을 넘긴다.

    메타데이터가 없으면 안내만 표시하고 "불러오기" 버튼은 비활성화된다.
    """

    def __init__(self, i18n: I18nManager, path: str | Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self.path = Path(path)
        self.metadata = read_metadata(self.path)
        self.settings: ReusableSettings = extract_reusable(self.metadata)

        tr = i18n.get_text
        self.setWindowTitle(tr("image_info.title"))
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        header = QLabel(self.path.name)
        header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.setToolTip(str(self.path))
        layout.addWidget(header)

        if self.settings.is_empty:
            notice = QLabel(tr("image_info.no_metadata"))
            notice.setWordWrap(True)
            layout.addWidget(notice)
        else:
            layout.addLayout(self._build_form())

        raw_label = QLabel(tr("image_info.field_raw"))
        layout.addWidget(raw_label)
        raw_view = QPlainTextEdit(self._raw_text())
        raw_view.setReadOnly(True)
        layout.addWidget(raw_view, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText(tr("image_info.apply"))
        self.apply_button.setEnabled(not self.settings.is_empty)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("image_info.close"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_form(self) -> QFormLayout:
        tr = self._i18n.get_text
        s = self.settings
        form = QFormLayout()

        def add(key: str, value: object) -> None:
            if value in (None, ""):
                return
            label = QLabel(str(value))
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(tr(key), label)

        add("image_info.field_prompt", s.prompt)
        add("image_info.field_negative", s.negative_prompt)
        add("image_info.field_seed", s.seed)
        add("image_info.field_model", (self.metadata or {}).get("model"))
        if s.width and s.height:
            add("image_info.field_size", f"{s.width} × {s.height}")
        add("image_info.field_steps", s.steps)
        add("image_info.field_scale", s.cfg_scale)
        add("image_info.field_sampler", s.sampler)
        if s.characters:
            summary = "\n".join(
                f"{i}. {c.prompt}" + (f"  (uc: {c.uc})" if c.uc else "")
                for i, c in enumerate(s.characters, start=1)
            )
            add("image_info.field_characters", summary)
        return form

    def _raw_text(self) -> str:
        if not self.metadata:
            return self._i18n.get_text("image_info.no_metadata")
        comment = self.metadata.get("comment")
        if isinstance(comment, dict):
            return json.dumps(comment, indent=2, ensure_ascii=False)
        return json.dumps(self.metadata.get("raw", {}), indent=2, ensure_ascii=False)
