"""인터페이스 Options_Page (KEY=`"interface"`) — 언어 / i2i 패널 표시 / 섹션 접힘 초기화.

언어 전환 자체는 저장 시점에 셸(`OptionsDialog`)이 수행한다 (Req 6.2). 이 페이지는 드래프트의
`language` 값만 갱신한다. 섹션 접힘 초기화도 드래프트를 즉시 건드리지 않고 **내부 플래그**만
세운 뒤 `commit`에서 `draft.ui = UiState()`를 대입한다 — 저장하지 않고 취소하면 초기화도
함께 취소되어야 하기 때문이다 (Req 6.4, 드래프트 의미론).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings, UiState
from . import OptionsPage, register_page

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

        self.update_check_check = QCheckBox(self)
        root.addWidget(self.update_check_check)

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
        # 다시 열릴 때마다 초기화 요청은 백지에서 시작한다.
        self._reset_sections = False
        self.reset_status_label.setText("")

    def commit(self, draft: AppSettings) -> None:
        code = self.language_combo.currentData()
        if isinstance(code, str) and code:
            draft.language = code
        draft.show_image_source = self.image_source_check.isChecked()
        draft.check_updates_on_start = self.update_check_check.isChecked()
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
        self.update_check_check.setText(tr("updates.check_on_start"))
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
