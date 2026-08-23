"""로그 설정 Options_Page (KEY=`"log"`, Req 8.1, 8.2, 8.5, 8.6).

`debug_logging` 하나를 두 위젯(`debug_check`, `verbosity_combo`)이 함께 표시한다 (Req 8.2).
둘을 양방향으로 연결하면 서로를 다시 부르므로 `_syncing` 재진입 가드로 한쪽만 흐르게 한다.
`VERBOSITY`의 인덱스가 `int(debug_logging)`이라서 변환에 분기가 필요 없다.

`log_dir`은 빈 문자열이 "OS 표준 위치"를 뜻한다. 그 사실이 화면에서 보이도록 플레이스홀더로
`str(default_log_dir())`을 보여 준다 (Req 8.5). "로그 폴더 열기"는 지금 입력된 값을 반영한
유효 디렉터리를 만든 뒤 연다 (Req 8.6).

로그 재구성 자체는 저장 시점에 셸(`OptionsDialog`)이 `reconfigure_logging`으로 수행한다
(Req 8.3, 8.4, 8.7). 이 페이지는 드래프트 값만 갱신한다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings, default_log_dir
from . import OptionsPage, open_in_file_manager, register_page

#: 로그 상세 수준 표기 — 인덱스가 곧 `int(debug_logging)`이다 (Req 8.2).
VERBOSITY: tuple[str, ...] = ("NORMAL", "DETAILED")

__all__ = ["VERBOSITY", "LoggingPage"]


@register_page
class LoggingPage(OptionsPage):
    """디버그 모드 / 상세 수준 / 로그 폴더 / 응답 헤더 / 크레딧 측정 (Req 8.1)."""

    KEY = "log"

    def __init__(
        self,
        i18n: I18nManager,
        parent: QWidget | None = None,
        **_extra: object,
    ) -> None:
        # `**_extra`: 셸이 모든 페이지에 같은 키워드 인자 집합을 넘겨도 되도록 남는 것은 무시한다.
        super().__init__(parent)
        self._i18n = i18n
        self._syncing = False  # debug_check ↔ verbosity_combo 재진입 가드

        root = QVBoxLayout(self)

        self.debug_check = QCheckBox(self)
        self.debug_check.toggled.connect(self._on_debug_toggled)
        root.addWidget(self.debug_check)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self.verbosity_label = QLabel(self)
        self.verbosity_combo = QComboBox(self)
        self.verbosity_combo.addItems(VERBOSITY)  # 로그 레벨 표기는 번역하지 않는다
        self.verbosity_combo.currentIndexChanged.connect(self._on_verbosity_changed)
        grid.addWidget(self.verbosity_label, 0, 0)
        grid.addWidget(self.verbosity_combo, 0, 1, 1, 2)

        self.log_dir_label = QLabel(self)
        self.log_dir_edit = QLineEdit(self)
        self.log_dir_edit.setPlaceholderText(str(default_log_dir()))  # 빈 값 = OS 표준 (Req 8.5)
        self.browse_button = QPushButton(self)
        self.browse_button.clicked.connect(self._browse)
        self.open_button = QPushButton(self)
        self.open_button.clicked.connect(self._open_log_folder)
        grid.addWidget(self.log_dir_label, 1, 0)
        grid.addWidget(self.log_dir_edit, 1, 1)
        grid.addWidget(self.browse_button, 1, 2)
        grid.addWidget(self.open_button, 1, 3)
        root.addLayout(grid)

        self.headers_check = QCheckBox(self)
        root.addWidget(self.headers_check)
        self.measure_check = QCheckBox(self)
        root.addWidget(self.measure_check)

        root.addStretch(1)
        self.retranslate()

    # ── 드래프트 ↔ 위젯 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self._set_debug(draft.debug_logging)
        self.log_dir_edit.setText(draft.log_dir)
        self.headers_check.setChecked(draft.debug_headers)
        self.measure_check.setChecked(draft.measure_credit)

    def commit(self, draft: AppSettings) -> None:
        draft.debug_logging = self.debug_check.isChecked()
        # 빈 값은 그대로 남긴다 — "OS 표준 위치"라는 의미가 있다 (Req 8.5).
        draft.log_dir = self.log_dir_edit.text().strip()
        draft.debug_headers = self.headers_check.isChecked()
        draft.measure_credit = self.measure_check.isChecked()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.debug_check.setText(tr("logs.debug_mode"))
        self.debug_check.setToolTip(tr("logs.debug_hint"))
        self.verbosity_label.setText(tr("options.verbosity"))
        self.log_dir_label.setText(tr("options.log_dir"))
        self.browse_button.setText(tr("options.browse"))
        self.open_button.setText(tr("options.open_log_folder"))
        self.headers_check.setText(tr("options.debug_headers"))
        self.measure_check.setText(tr("logs.measure_credit"))
        self.measure_check.setToolTip(tr("logs.measure_credit_hint"))

    # ── 두 위젯 한 값 (Req 8.2) ────────────────────────────────────────

    def _set_debug(self, on: bool) -> None:
        """가드를 걸고 두 위젯을 같은 값으로 맞춘다."""
        self._syncing = True
        try:
            self.debug_check.setChecked(on)
            self.verbosity_combo.setCurrentIndex(int(on))
        finally:
            self._syncing = False

    def _on_debug_toggled(self, on: bool) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.verbosity_combo.setCurrentIndex(int(on))
        finally:
            self._syncing = False

    def _on_verbosity_changed(self, index: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.debug_check.setChecked(bool(index))
        finally:
            self._syncing = False

    # ── 로그 폴더 ─────────────────────────────────────────────────────

    def _effective_log_dir(self) -> Path:
        """지금 입력된 값의 유효 로그 디렉터리.

        `AppSettings.log_dir_path()`와 같은 규칙(빈 값 → OS 표준)을 아직 커밋되지 않은 입력란
        값에 적용한다 — 찾아보기 시작 위치와 "로그 폴더 열기" 대상이 화면과 일치해야 한다
        (Req 8.5, 8.6).
        """
        text = self.log_dir_edit.text().strip()
        return Path(text) if text else default_log_dir()

    def _browse(self) -> None:
        tr = self._i18n.get_text
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("options.choose_folder", tr("options.log_dir")),
            str(self._effective_log_dir()),
        )
        if chosen:
            self.log_dir_edit.setText(str(Path(chosen).resolve()))

    def _open_log_folder(self) -> None:
        """현재 로그 디렉터리를 만든 뒤 OS 파일 탐색기로 연다 (Req 8.6)."""
        open_in_file_manager(self._effective_log_dir(), self._i18n.get_text, parent=self)
