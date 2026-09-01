"""인터페이스 Options_Page (KEY=`"interface"`) — 언어 / i2i 패널 표시 / 섹션 접힘 초기화.

언어 전환 자체는 저장 시점에 셸(`OptionsDialog`)이 수행한다 (Req 6.2). 이 페이지는 드래프트의
`language` 값만 갱신한다. 섹션 접힘 초기화도 드래프트를 즉시 건드리지 않고 **내부 플래그**만
세운 뒤 `commit`에서 `draft.ui = UiState()`를 대입한다 — 저장하지 않고 취소하면 초기화도
함께 취소되어야 하기 때문이다 (Req 6.4, 드래프트 의미론).
"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings, UiState
from . import OptionsPage, register_page

#: 0 = 폰트 크기를 바꾸지 않는다 (Qt/테마 기본값 유지).
FONT_SIZE_RANGE = (0, 48)

#: prompt_font 필드명 → (라벨 i18n 키, 색상 다이얼로그 제목 i18n 키). 셋 다 "" = 기본값이라
#: 저장/복원/버튼 배선이 같은 모양이라 표로 두고 한 번에 돌린다.
COLOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("color", "options.prompt_font_color"),
    ("emphasis_color", "options.prompt_emphasis_color"),
    ("deemphasis_color", "options.prompt_deemphasis_color"),
)

__all__ = ["InterfacePage"]


@register_page
class InterfacePage(OptionsPage):
    """언어 선택 + i2i 패널 표시 + 메인 UI 섹션 접힘 상태 초기화 (Req 6.1)."""

    KEY = "interface"

    def __init__(
        self,
        i18n: I18nManager,
        *,
        supports_i2i: bool = True,
        parent: QWidget | None = None,
        **_extra: object,
    ) -> None:
        # `**_extra`: 셸이 모든 페이지에 같은 키워드 인자 집합을 넘겨도 되도록 남는 것은 무시한다.
        super().__init__(parent)
        self._i18n = i18n
        self._supports_i2i = supports_i2i
        self._reset_sections = False  # commit에서 UiState()를 대입할지 여부 (Req 6.4)
        self._colors: dict[str, str] = {field: "" for field, _label_key in COLOR_FIELDS}  # "" = 기본값

        root = QVBoxLayout(self)

        form = QFormLayout()
        self.language_label = QLabel(self)
        self.language_combo = QComboBox(self)
        for code, name in i18n.get_available_languages().items():
            self.language_combo.addItem(name, code)
        form.addRow(self.language_label, self.language_combo)
        root.addLayout(form)

        self.image_source_check = QCheckBox(self)
        self.image_source_check.setEnabled(supports_i2i)
        root.addWidget(self.image_source_check)

        # 강화 패널도 img2img 경로를 쓰므로 i2i를 지원하는 모델에서만 켤 수 있다.
        self.enhance_check = QCheckBox(self)
        self.enhance_check.setEnabled(supports_i2i)
        root.addWidget(self.enhance_check)

        self.update_check_check = QCheckBox(self)
        root.addWidget(self.update_check_check)

        font_form = QFormLayout()
        self.font_size_label = QLabel(self)
        self.font_size_spin = QSpinBox(self)
        self.font_size_spin.setRange(*FONT_SIZE_RANGE)
        font_form.addRow(self.font_size_label, self.font_size_spin)

        #: 필드명 → (라벨, 색상 버튼, 기본값 버튼). load/commit/retranslate가 함께 순회한다.
        self.color_labels: dict[str, QLabel] = {}
        self.color_buttons: dict[str, QPushButton] = {}
        self.color_reset_buttons: dict[str, QPushButton] = {}
        for field, _label_key in COLOR_FIELDS:
            label = QLabel(self)
            row = QHBoxLayout()
            button = QPushButton(self)
            button.clicked.connect(partial(self._pick_color, field))
            reset_button = QPushButton(self)
            reset_button.clicked.connect(partial(self._reset_color, field))
            row.addWidget(button)
            row.addWidget(reset_button)
            font_form.addRow(label, row)
            self.color_labels[field] = label
            self.color_buttons[field] = button
            self.color_reset_buttons[field] = reset_button
        root.addLayout(font_form)

        reset_row = QHBoxLayout()
        self.reset_sections_button = QPushButton(self)
        self.reset_sections_button.clicked.connect(self._on_reset_sections)
        reset_row.addWidget(self.reset_sections_button)
        self.reset_status_label = QLabel(self)
        self.reset_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        reset_row.addWidget(self.reset_status_label, 1)
        root.addLayout(reset_row)

        root.addStretch(1)
        self.retranslate()

    # ── 드래프트 ↔ 위젯 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.update_check_check.setChecked(draft.check_updates_on_start)
        index = self.language_combo.findData(draft.language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        self.image_source_check.setChecked(draft.show_image_source)
        self.enhance_check.setChecked(draft.show_enhance)
        self.font_size_spin.setValue(draft.prompt_font.size)
        for field, _label_key in COLOR_FIELDS:
            self._colors[field] = getattr(draft.prompt_font, field)
            self._refresh_color_button(field)
        # 다시 열릴 때마다 초기화 요청은 백지에서 시작한다.
        self._reset_sections = False
        self.reset_status_label.setText("")

    def commit(self, draft: AppSettings) -> None:
        code = self.language_combo.currentData()
        if isinstance(code, str) and code:
            draft.language = code
        draft.show_image_source = self.image_source_check.isChecked()
        draft.show_enhance = self.enhance_check.isChecked()
        draft.check_updates_on_start = self.update_check_check.isChecked()
        draft.prompt_font.size = self.font_size_spin.value()
        for field, _label_key in COLOR_FIELDS:
            setattr(draft.prompt_font, field, self._colors[field])
        if self._reset_sections:
            draft.ui = UiState()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.language_label.setText(tr("menu.languages"))
        # 언어 이름은 각 언어 파일의 `language_name`(자국어 표기)이라 번역 대상이 아니지만,
        # 언어 파일이 다시 로드됐을 수도 있으니 선택을 유지한 채 표기만 갱신한다.
        names = self._i18n.get_available_languages()
        for row in range(self.language_combo.count()):
            code = self.language_combo.itemData(row)
            if isinstance(code, str) and code in names:
                self.language_combo.setItemText(row, names[code])
        self.image_source_check.setText(tr("image_source.menu"))
        self.image_source_check.setToolTip("" if self._supports_i2i else tr("image_source.unsupported"))
        self.enhance_check.setText(tr("enhance.menu"))
        self.enhance_check.setToolTip("" if self._supports_i2i else tr("image_source.unsupported"))
        self.update_check_check.setText(tr("updates.check_on_start"))
        self.font_size_label.setText(tr("options.prompt_font_size"))
        self.font_size_spin.setSpecialValueText(tr("options.prompt_font_size_default"))
        for field, label_key in COLOR_FIELDS:
            self.color_labels[field].setText(tr(label_key))
            self.color_buttons[field].setText(tr("options.choose_color"))
            self.color_reset_buttons[field].setText(tr("options.reset_to_default"))
        self.reset_sections_button.setText(tr("options.reset_sections"))
        if self._reset_sections:
            self.reset_status_label.setText(tr("options.reset_sections_done"))

    # ── 내부 ──────────────────────────────────────────────────────────

    def _on_reset_sections(self) -> None:
        self._reset_sections = True
        self.reset_status_label.setText(self._i18n.get_text("options.reset_sections_done"))

    def reset_sections_requested(self) -> bool:
        """저장 시 `ui`를 기본값으로 되돌릴 예정인지 (테스트·셸 조회용)."""
        return self._reset_sections

    def _pick_color(self, field: str) -> None:
        current = self._colors[field]
        initial = QColor(current) if current else QColor(Qt.GlobalColor.white)
        label_key = next(key for f, key in COLOR_FIELDS if f == field)
        color = QColorDialog.getColor(initial, self, self._i18n.get_text(label_key))
        if color.isValid():
            self._colors[field] = color.name()
            self._refresh_color_button(field)

    def _reset_color(self, field: str) -> None:
        self._colors[field] = ""
        self._refresh_color_button(field)

    def _refresh_color_button(self, field: str) -> None:
        color = self._colors[field]
        self.color_buttons[field].setStyleSheet(f"background-color: {color};" if color else "")
