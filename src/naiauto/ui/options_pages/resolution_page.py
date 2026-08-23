"""해상도 Options_Page (Req 5.3, 5.6).

등급 활성 체크박스 2개 + 크레딧 경고 문구 + 커스텀 해상도 6행이다. 카탈로그 계산은
`core.resolution_catalog`, 값 검증은 `core.settings.validation`이 하고 이 모듈은 배선만 한다.

여기서는 64의 배수로 **조용히 보정하지 않는다** (Req 5.8): 사용자가 등록한 프리셋이 말없이
바뀌면 안 되므로 잘못된 값은 저장 시 `validate_options`가 오류로 돌려세운다. 메인 윈도우의
Resolution_Panel은 반대로 조용히 스냅한다 (Req 10.12) — 생성이 막히면 안 되기 때문이다.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.resolution_catalog import DIMENSION_STEP, MAX_DIMENSION, MIN_DIMENSION
from ...core.settings.schema import CUSTOM_RESOLUTION_SLOTS, AppSettings, CustomResolution
from ..widgets.wheel_guard import guard_wheel
from . import OptionsPage, register_page

__all__ = ["CustomResolutionRow", "ResolutionPage"]


class CustomResolutionRow:
    """커스텀 해상도 1행의 위젯 묶음 (`resolution.customs[i]`와 1:1)."""

    def __init__(self, index: int, parent: QWidget) -> None:
        self.index = index
        self.enabled_check = QCheckBox(parent)
        self.width_spin = _make_dimension_spin(parent)
        self.height_spin = _make_dimension_spin(parent)
        self.times_label = QLabel("×", parent)

    def load(self, row: CustomResolution) -> None:
        self.enabled_check.setChecked(row.enabled)
        # 손으로 편집한 settings.json의 범위 밖 값은 QSpinBox가 클램프한다. 64의 배수가 아닌
        # 값은 그대로 남아 저장 시 검증에 걸린다 (Req 5.8).
        self.width_spin.setValue(row.width)
        self.height_spin.setValue(row.height)

    def value(self) -> CustomResolution:
        return CustomResolution(
            enabled=self.enabled_check.isChecked(),
            width=self.width_spin.value(),
            height=self.height_spin.value(),
        )

    def widgets(self) -> tuple[QWidget, ...]:
        return (self.enabled_check, self.width_spin, self.times_label, self.height_spin)


def _make_dimension_spin(parent: QWidget) -> QSpinBox:
    spin = QSpinBox(parent)
    spin.setRange(MIN_DIMENSION, MAX_DIMENSION)
    spin.setSingleStep(DIMENSION_STEP)
    return spin


@register_page
class ResolutionPage(OptionsPage):
    """`Large` / `Wallpaper` 등급 토글과 커스텀 해상도 6행."""

    KEY = "resolution"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n

        layout = QVBoxLayout(self)

        self.large_check = QCheckBox(self)
        self.wallpaper_check = QCheckBox(self)
        layout.addWidget(self.large_check)
        layout.addWidget(self.wallpaper_check)

        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c07a00;")
        layout.addWidget(self.warning_label)

        self.custom_group = QGroupBox(self)
        group_layout = QVBoxLayout(self.custom_group)
        self.custom_desc_label = QLabel(self.custom_group)
        self.custom_desc_label.setWordWrap(True)
        self.custom_desc_label.setStyleSheet("color: palette(mid);")
        group_layout.addWidget(self.custom_desc_label)

        grid = QGridLayout()
        self.rows: tuple[CustomResolutionRow, ...] = tuple(
            CustomResolutionRow(index, self.custom_group) for index in range(CUSTOM_RESOLUTION_SLOTS)
        )
        for row in self.rows:
            grid.addWidget(row.enabled_check, row.index, 0)
            grid.addWidget(row.width_spin, row.index, 1)
            grid.addWidget(row.times_label, row.index, 2)
            grid.addWidget(row.height_spin, row.index, 3)
        grid.setColumnStretch(4, 1)
        group_layout.addLayout(grid)
        layout.addWidget(self.custom_group)
        layout.addStretch(1)

        # 스크롤 중 값이 바뀌는 사고 방지 (기존 관습)
        self._wheel_guard = guard_wheel(self)

        self.retranslate()

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        options = draft.resolution
        self.large_check.setChecked(options.enable_large)
        self.wallpaper_check.setChecked(options.enable_wallpaper)
        for index, row in enumerate(self.rows):
            # 슬롯 수보다 짧은 목록(옛 설정)은 기본값 행으로 채운다.
            source = options.customs[index] if index < len(options.customs) else CustomResolution()
            row.load(source)

    def commit(self, draft: AppSettings) -> None:
        draft.resolution.enable_large = self.large_check.isChecked()
        draft.resolution.enable_wallpaper = self.wallpaper_check.isChecked()
        draft.resolution.customs = [row.value() for row in self.rows]

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.large_check.setText(tr("options.enable_large_resolution"))
        self.large_check.setToolTip(tr("options.enable_large_resolution_tooltip"))
        self.wallpaper_check.setText(tr("options.enable_wallpaper_resolution"))
        self.wallpaper_check.setToolTip(tr("options.enable_wallpaper_resolution_tooltip"))
        self.warning_label.setText(tr("options.anlas_resolution_warning"))
        self.custom_group.setTitle(tr("options.custom_resolutions_title"))
        self.custom_desc_label.setText(tr("options.custom_resolutions_desc"))
        for row in self.rows:
            row.enabled_check.setText(tr("options.custom_resolution_n", row.index + 1))
            row.width_spin.setToolTip(tr("resolution.width"))
            row.height_spin.setToolTip(tr("resolution.height"))
