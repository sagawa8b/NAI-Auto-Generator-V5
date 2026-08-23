"""연속 생성 옵션 페이지 (KEY="generation", Req 4.1).

`batch.count`와 `batch.delay_seconds` 두 값만 다룬다. 스핀박스 범위가 이미 값을 막지만
손으로 편집한 `settings.json`도 걸러야 하므로 검증은 `core.settings.validation`이 한 번 더
한다 (Req 4.3, 4.4). `stop_on_anlas_error`는 이 스펙의 범위 밖이라 노출하지 않는다 — 드래프트의
값을 그대로 남겨 둔다.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QLabel, QSpinBox, QWidget

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings
from ...core.settings.validation import BATCH_COUNT_RANGE, BATCH_DELAY_RANGE
from ..widgets.wheel_guard import guard_wheel
from . import OptionsPage, register_page

DELAY_STEP = 0.5

__all__ = ["BatchPage"]


@register_page
class BatchPage(OptionsPage):
    """연속 생성 매수·간격."""

    KEY = "generation"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n

        layout = QFormLayout(self)

        self.count_label = QLabel()
        self.count_spin = QSpinBox()
        self.count_spin.setRange(*BATCH_COUNT_RANGE)  # 0 = 무한 (Req 4.1)
        layout.addRow(self.count_label, self.count_spin)

        self.delay_label = QLabel()
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(*BATCH_DELAY_RANGE)
        self.delay_spin.setSingleStep(DELAY_STEP)
        # 소수 자릿수는 Qt 기본값(2)을 그대로 쓴다 — 메인 윈도우의 간격 스핀박스와 같다.
        layout.addRow(self.delay_label, self.delay_spin)

        # 스크롤 중 값이 바뀌는 사고 방지 (기존 관습)
        self._wheel_guard = guard_wheel(self)

        self.retranslate()

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.count_spin.setValue(draft.batch.count)
        self.delay_spin.setValue(draft.batch.delay_seconds)

    def commit(self, draft: AppSettings) -> None:
        draft.batch.count = self.count_spin.value()
        draft.batch.delay_seconds = self.delay_spin.value()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.count_label.setText(tr("batch.count"))
        self.delay_label.setText(tr("batch.delay"))
