"""LLM Options_Page (KEY=`"llm"`) — 자연어 프롬프트 생성용 LM Studio 연결 설정.

Host / 모델 / 타임아웃 / 시스템 프롬프트 / 기본 반영 방식을 다룬다. "연결 테스트"와
"모델 새로고침"은 지금 켜져 있는 LM Studio 서버에 실제로 붙어 본다 — 저장 전에도
사용자가 설정이 맞는지 바로 확인할 수 있게. 무거운 `lmstudio` import는 버튼을 눌렀을
때만 일어난다 (`core.llm.runtime_error` 참고).
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.llm.lmstudio_client import DEFAULT_HOST
from ...core.settings.schema import AppSettings
from . import OptionsPage, register_page

logger = logging.getLogger(__name__)

#: 타임아웃 스핀박스 범위 (core 검증의 LLM_TIMEOUT_RANGE와 맞춘다). 0 = 무제한.
TIMEOUT_RANGE = (0.0, 3600.0)

__all__ = ["LLMPage"]


@register_page
class LLMPage(OptionsPage):
    """LM Studio 연결 설정 페이지."""

    KEY = "llm"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None, **_extra: object) -> None:
        super().__init__(parent)
        self._i18n = i18n

        root = QVBoxLayout(self)

        form = QFormLayout()

        # Host + 연결 테스트
        self.host_label = QLabel(self)
        host_row = QHBoxLayout()
        self.host_edit = QLineEdit(self)
        self.host_edit.setPlaceholderText(DEFAULT_HOST)
        self.test_button = QPushButton(self)
        self.test_button.clicked.connect(self._test_connection)
        host_row.addWidget(self.host_edit, 1)
        host_row.addWidget(self.test_button)
        form.addRow(self.host_label, host_row)

        # 모델은 여기서 고르지 않는다 — 생성 다이얼로그가 서버에 로드된 목록에서 고른다.
        # (NAI가 모델을 저장하면 서버가 그 모델을 로드/유지하려다 복수 모델이 뜨는 문제가
        # 있었다.) 여기서는 안내 문구만 둔다.
        self.model_hint_label = QLabel(self)
        self.model_hint_label.setWordWrap(True)
        self.model_hint_label.setStyleSheet("color: palette(mid);")
        form.addRow("", self.model_hint_label)

        # 타임아웃
        self.timeout_label = QLabel(self)
        self.timeout_spin = QDoubleSpinBox(self)
        self.timeout_spin.setRange(*TIMEOUT_RANGE)
        self.timeout_spin.setDecimals(0)
        self.timeout_spin.setSingleStep(10)
        self.timeout_spin.setSuffix(" s")
        form.addRow(self.timeout_label, self.timeout_spin)

        root.addLayout(form)

        # 상태 문구 (연결 테스트/새로고침 결과)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # 기본 출력 스타일 (자연어 문장 / 단부루 태그)
        self.style_label = QLabel(self)
        self.style_label.setStyleSheet("font-weight: bold;")
        root.addWidget(self.style_label)
        style_row = QHBoxLayout()
        self.natural_radio = QRadioButton(self)
        self.danbooru_radio = QRadioButton(self)
        # 스타일 라디오 2개를 하나의 그룹으로 묶는다 — 그러지 않으면 같은 부모 아래의
        # 반영 방식 라디오와 섞여 넷 중 하나만 선택되는 자동 배타에 걸린다.
        self._style_group = QButtonGroup(self)
        self._style_group.addButton(self.natural_radio)
        self._style_group.addButton(self.danbooru_radio)
        style_row.addWidget(self.natural_radio)
        style_row.addWidget(self.danbooru_radio)
        style_row.addStretch(1)
        root.addLayout(style_row)

        # 기본 반영 방식
        self.apply_mode_label = QLabel(self)
        self.apply_mode_label.setStyleSheet("font-weight: bold;")
        root.addWidget(self.apply_mode_label)
        mode_row = QHBoxLayout()
        self.append_radio = QRadioButton(self)
        self.replace_radio = QRadioButton(self)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.append_radio)
        self._mode_group.addButton(self.replace_radio)
        mode_row.addWidget(self.append_radio)
        mode_row.addWidget(self.replace_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # 시스템 프롬프트 (선택)
        self.system_label = QLabel(self)
        root.addWidget(self.system_label)
        self.system_edit = QPlainTextEdit(self)
        self.system_edit.setFixedHeight(110)
        root.addWidget(self.system_edit)

        root.addStretch(1)
        self.retranslate()

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        cfg = draft.lmstudio
        self.host_edit.setText(cfg.host)
        self.timeout_spin.setValue(cfg.timeout_seconds)
        self.system_edit.setPlainText(cfg.system_prompt)
        if cfg.default_style == "danbooru":
            self.danbooru_radio.setChecked(True)
        else:
            self.natural_radio.setChecked(True)
        if cfg.default_apply_mode == "replace":
            self.replace_radio.setChecked(True)
        else:
            self.append_radio.setChecked(True)
        self.status_label.setText("")

    def commit(self, draft: AppSettings) -> None:
        cfg = draft.lmstudio
        cfg.host = self.host_edit.text().strip() or DEFAULT_HOST
        # 모델은 저장하지 않는다 (다이얼로그가 서버 목록에서 고른다). 기존 값은 건드리지 않는다.
        cfg.timeout_seconds = self.timeout_spin.value()
        cfg.system_prompt = self.system_edit.toPlainText().strip()
        cfg.default_style = "danbooru" if self.danbooru_radio.isChecked() else "natural"
        cfg.default_apply_mode = "replace" if self.replace_radio.isChecked() else "append"

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.host_label.setText(tr("options.llm_host"))
        self.test_button.setText(tr("options.llm_test"))
        self.model_hint_label.setText(tr("options.llm_model_hint"))
        self.timeout_label.setText(tr("options.llm_timeout"))
        self.style_label.setText(tr("options.llm_style"))
        self.natural_radio.setText(tr("options.llm_style_natural"))
        self.danbooru_radio.setText(tr("options.llm_style_danbooru"))
        self.apply_mode_label.setText(tr("options.llm_apply_mode"))
        self.append_radio.setText(tr("options.llm_apply_append"))
        self.replace_radio.setText(tr("options.llm_apply_replace"))
        self.system_label.setText(tr("options.llm_system_prompt"))
        self.system_edit.setPlaceholderText(tr("options.llm_system_prompt_hint"))

    # ── 내부: 실서버 확인 ────────────────────────────────────────────────

    def _host(self) -> str:
        return self.host_edit.text().strip() or DEFAULT_HOST

    def _test_connection(self) -> None:
        """지금 켜진 LM Studio 서버에 붙어 본다 (`is_valid_api_host`)."""
        tr = self._i18n.get_text
        from ...core.llm.lmstudio_client import LMStudioPromptGenerator, runtime_error

        failure = runtime_error()
        if failure:
            self.status_label.setText(tr("options.llm_not_installed", failure))
            return
        try:
            ok = LMStudioPromptGenerator.check_connection(self._host())
        except Exception as e:  # noqa: BLE001
            logger.debug("LM Studio connection test failed: %s", e)
            self.status_label.setText(tr("options.llm_connect_failed", self._host()))
            return
        if ok:
            self.status_label.setText(tr("options.llm_connect_ok", self._host()))
        else:
            self.status_label.setText(tr("options.llm_connect_failed", self._host()))
