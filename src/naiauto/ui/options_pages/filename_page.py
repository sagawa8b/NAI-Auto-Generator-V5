"""파일명 설정 Options_Page (Req 3.1, 3.2, 3.9).

토큰 목록과 치환 규칙은 전부 `core.metadata.save`에 있다. 이 모듈은 조립·배선만 한다:
토큰 도움말은 `TOKEN_NAMES`를 순회해 만들고(UI에 토큰을 두 번 적지 않는다), 미리보기는
`preview_filename`을 그대로 부른다.

범위 검증(1–100, 빈 템플릿, 알려진 토큰 없음)은 저장 시점에
`core.settings.validation.validate_options`가 담당한다 (Req 3.10, 3.11) — 여기서 다시 하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout, QWidget

from ...core.i18n.manager import I18nManager
from ...core.metadata.save import (
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_WORD_LIMIT,
    IMAGE_FORMATS,
    SAMPLE_CONTEXT,
    TOKEN_NAMES,
    preview_filename,
    token_values,
)
from ...core.settings.schema import AppSettings
from ...core.settings.validation import WORD_LIMIT_RANGE
from . import OptionsPage, register_page

logger = logging.getLogger(__name__)

#: 미리보기를 만들 수 없을 때 표시하는 자리표시자 (Req 3.9).
PREVIEW_UNAVAILABLE = "—"

__all__ = ["PREVIEW_UNAVAILABLE", "FilenamePage"]


@register_page
class FilenamePage(OptionsPage):
    """`filename_template` + 단어 수 제한 2개 + 토큰 도움말 + 미리보기 (Req 3.1)."""

    KEY = "filename"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.template_label = QLabel()
        self.template_edit = QLineEdit()
        form.addRow(self.template_label, self.template_edit)

        low, high = WORD_LIMIT_RANGE
        self.prompt_limit_label = QLabel()
        self.prompt_limit_spin = self._make_limit_spin(low, high)
        form.addRow(self.prompt_limit_label, self.prompt_limit_spin)

        self.character_limit_label = QLabel()
        self.character_limit_spin = self._make_limit_spin(low, high)
        form.addRow(self.character_limit_label, self.character_limit_spin)

        self.format_label = QLabel()
        self.format_combo = QComboBox()
        for fmt in IMAGE_FORMATS:
            self.format_combo.addItem("", fmt)  # 라벨은 retranslate()에서 채운다
        form.addRow(self.format_label, self.format_combo)
        self.format_hint = QLabel()
        self.format_hint.setWordWrap(True)
        self.format_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.format_hint)

        # 길이 상한은 미리보기만 봐서는 알 수 없다 — 예시 프롬프트가 짧아 상한에 안 걸린다.
        self.length_hint = QLabel()
        self.length_hint.setWordWrap(True)
        self.length_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.length_hint)

        self.preview_label = QLabel()
        self.preview_value = QLabel()  # 읽기 전용 (Req 3.9)
        self.preview_value.setWordWrap(True)
        form.addRow(self.preview_label, self.preview_value)
        layout.addLayout(form)

        self.token_help = QLabel()  # 읽기 전용 (Req 3.2)
        self.token_help.setWordWrap(True)
        self.token_help.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.token_help)

        layout.addStretch(1)

        # 세 위젯 중 무엇이 바뀌어도 미리보기를 다시 만든다 (Req 3.9).
        self.template_edit.textChanged.connect(self._refresh_preview)
        self.prompt_limit_spin.valueChanged.connect(self._refresh_preview)
        self.character_limit_spin.valueChanged.connect(self._refresh_preview)
        self.format_combo.currentIndexChanged.connect(self._refresh_preview)

        self.retranslate()

    @staticmethod
    def _make_limit_spin(low: int, high: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(DEFAULT_WORD_LIMIT)
        return spin

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.template_edit.setText(draft.filename_template)
        self.prompt_limit_spin.setValue(draft.prompt_word_limit)
        self.character_limit_spin.setValue(draft.character_word_limit)
        index = self.format_combo.findData(draft.image_format)
        self.format_combo.setCurrentIndex(
            index if index >= 0 else self.format_combo.findData(DEFAULT_IMAGE_FORMAT)
        )
        self._refresh_preview()

    def commit(self, draft: AppSettings) -> None:
        """정규화하지 않는다 — 빈 템플릿·토큰 없음은 검증이 오류로 알린다 (Req 3.11)."""
        draft.filename_template = self.template_edit.text()
        draft.prompt_word_limit = self.prompt_limit_spin.value()
        draft.character_word_limit = self.character_limit_spin.value()
        draft.image_format = self.format_combo.currentData() or DEFAULT_IMAGE_FORMAT

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.template_label.setText(tr("options.filename_template"))
        self.prompt_limit_label.setText(tr("options.prompt_word_limit"))
        self.character_limit_label.setText(tr("options.character_word_limit"))
        self.format_label.setText(tr("options.image_format"))
        for i, fmt in enumerate(IMAGE_FORMATS):
            self.format_combo.setItemText(i, tr(f"options.image_format_{fmt}"))
        self.format_hint.setText(tr("options.image_format_hint"))
        self.length_hint.setText(tr("options.filename_length_hint"))
        self.preview_label.setText(tr("options.preview"))
        self.token_help.setText(self._token_help_text())
        self._refresh_preview()

    # ── 내부 ────────────────────────────────────────────────────────────

    def _token_help_text(self) -> str:
        """지원 토큰 + 치환 예시를 한 줄씩 (Req 3.2). 목록의 출처는 TOKEN_NAMES 하나다."""
        tr = self._i18n.get_text
        values = token_values(SAMPLE_CONTEXT, datetime.now())
        lines = [tr("options.token_help_title")]
        lines += [f"{{{name}}} — {tr(f'options.token_{name}')} (→ {values[name]})" for name in TOKEN_NAMES]
        return "\n".join(lines)

    def _refresh_preview(self) -> None:
        """고정 예시 값으로 만든 파일명 미리보기 (Req 3.9). 실패하면 자리표시자."""
        try:
            stem = preview_filename(
                self.template_edit.text(),
                prompt_word_limit=self.prompt_limit_spin.value(),
                character_word_limit=self.character_limit_spin.value(),
            )
        except Exception:  # 템플릿이 무엇이든 미리보기가 다이얼로그를 죽이면 안 된다.
            logger.debug("filename preview failed", exc_info=True)
            self.preview_value.setText(PREVIEW_UNAVAILABLE)
            return
        fmt = self.format_combo.currentData() or DEFAULT_IMAGE_FORMAT
        self.preview_value.setText(f"{stem}.{fmt}")
