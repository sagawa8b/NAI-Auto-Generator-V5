"""메인 윈도우 — 얇은 뷰: 위젯 조립 + 이벤트 표시만.

원칙 (PLANNING.md):
  - 모델/샘플러/스케줄러/해상도/UC 프리셋 콤보는 전부 ModelSpec에서 채운다.
  - 생성 시작 시 위젯 상태를 불변 GenerationJob으로 1회 스냅숏 — 이후 워커는
    위젯을 만지지 않는다.
  - 서비스 이벤트는 QtEventBridge 시그널로만 수신한다.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
from collections.abc import Callable
from pathlib import Path

import platformdirs
import shiboken6
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal, SignalInstance
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import settings_file
from ..core.api.client import NAIClient
from ..core.api.model_specs import MODEL_REGISTRY, ModelSpec, get_spec
from ..core.api.models import CharacterCaption, GenerationRequest
from ..core.api.subscription import OpusUsage
from ..core.credit_estimator import CreditEstimator
from ..core.i18n.manager import I18nManager
from ..core.logging_setup import configure_logging, crash_log_path, log_path
from ..core.metadata.reuse import ReusableSettings
from ..core.presets import CharacterPromptPreset, GenerationPreset, PresetError, PresetStore
from ..core.resolution_catalog import ResolutionCatalog
from ..core.settings import accounts, credentials
from ..core.settings.schema import APP_NAME, QUICK_COUNT_SLOTS, AppSettings, CharacterPromptState
from ..core.settings.store import ensure_dirs
from ..core.tag_completer import TagCompleter, resolve_database_path
from ..core.updates import RELEASES_PAGE, ReleaseInfo, check_for_update
from ..services.events import (
    GenerationEvent,
    ImageCompleted,
    ImageRetrying,
    ImageStarted,
    JobFinished,
    JobStarted,
    WaitingNext,
)
from ..services.generation_service import GenerationJob, GenerationService
from .accounts_dialog import AccountsDialog
from .gallery_view import GalleryView
from .image_info_dialog import ImageInfoDialog
from .log_dialog import LogDialog
from .login_dialog import LoginDialog
from .options_dialog import OptionsDialog
from .options_pages import open_in_file_manager
from .preset_manager_dialog import PresetManagerDialog
from .qt_bridge import QtEventBridge
from .tag_completer_dropdown import TagCompleterDropdown
from .widgets.character_prompts import CharacterPromptsWidget, CharacterSlot
from .widgets.collapsible_section import CollapsibleSection, compose_ai_summary
from .widgets.image_source import ImageSourceWidget
from .widgets.prompt_tabs import PromptTabs
from .widgets.resize_handle import ResizeHandle
from .widgets.resolution_panel import ResolutionPanel
from .widgets.status_bar_gauge import StatusBarGauge
from .widgets.wheel_guard import guard_wheel
from .widgets.zoomable_image_view import ZoomableImageView

logger = logging.getLogger(__name__)

_ERROR_TYPE_TO_KEY = {
    "AuthError": "errors.auth_error",
    "InsufficientAnlasError": "errors.payment_required",
    "NetworkError": "errors.network_error",
    "RateLimitError": "errors.api_error",
    "PayloadRejectedError": "errors.api_error",
    "ServerBusyError": "errors.network_error",
    "FixedSeedBatchError": "errors.fixed_seed_batch",
}


WINDOW_SIZE = (1180, 760)
SPLITTER_SIZES = (460, 720)  # 입력 패널 / 결과 패널


def _format_seconds(value: float) -> str:
    """3.0 → "3", 2.5 → "2.5" (상태바에 소수점이 지저분하게 남지 않도록)."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _format_duration(seconds: int) -> str:
    """7888 → "2h 11m" (크레딧 충전까지 남은 시간)."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


class MainWindow(QMainWindow):
    _anlas_fetched = Signal(object)  # dict | Exception — 워커 스레드에서 emit
    _update_checked = Signal(object, bool)  # ReleaseInfo | None, 사용자가 직접 눌렀는가

    def __init__(
        self,
        i18n: I18nManager,
        settings: AppSettings,
        client: NAIClient,
        service: GenerationService,
        bridge: QtEventBridge,
    ) -> None:
        super().__init__()
        self._i18n = i18n
        self._settings = settings
        self._client = client
        self._service = service

        bridge.event_received.connect(self._on_generation_event)
        self._anlas_fetched.connect(self._on_anlas_fetched)
        self._update_checked.connect(self._on_update_checked)
        i18n.subscribe(self._on_language_changed)

        # M3: Credit Estimator — compute remaining images from credit observations
        data_dir = Path(platformdirs.user_data_dir(APP_NAME))
        self._credit_estimator = CreditEstimator(data_dir)
        self._credit_estimator.load()

        # M3: Tag Completer — 실제 로드와 드롭다운 부착은 _refresh_tag_completer()가 한다
        self._tag_completer = TagCompleter()

        # M3: Preset Store — named parameter snapshots (경로는 옵션에서 바꿀 수 있다, Req 2.5)
        self._preset_store = PresetStore(Path(settings.presets_dir))

        #: 로그인 상태. 생성 버튼 활성 조건에 `_is_running`과 함께 들어간다.
        #: 실제 값은 창을 띄운 쪽이 `set_logged_in()`으로 정해 준다.
        self._logged_in = client.session.is_logged_in

        self._qsettings = QSettings()
        #: 세팅별 연속 생성이 순환할 파일 목록 — 비어 있으면 진행 중이 아니다.
        self._settings_batch_paths: list[str] = []

        self._build_ui()
        self._resize_handle.restore_height()
        self._restore_splitters()
        self._setup_m3_components()
        self._populate_models()
        self._apply_settings()
        self.retranslate()
        # 창 스스로 로그인 상태와 버튼을 맞춰 둔다. 호출한 쪽이 set_logged_in()을
        # 부르지 않아도 어긋나지 않게 (로그아웃 상태로 뜰 수 있다).
        self._refresh_generate_buttons()
        self.refresh_anlas()
        if settings.check_updates_on_start:
            self.check_for_updates(manual=False)

    # ── UI 조립 ───────────────────────────────────────────

    def _build_ui(self) -> None:
        splitter = self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)
        if not self._restore_window_geometry():
            self.resize(*WINDOW_SIZE)
        self.setAcceptDrops(True)  # PNG를 끌어다 놓으면 생성 정보 표시

        # 왼쪽: 입력
        left = QWidget()
        left_layout = QVBoxLayout(left)

        # 프롬프트 / Undesired Content — 탭 전환 (웹 UI·V4.5와 동일, 세로 공간 절약)
        self.prompt_group = QGroupBox()
        pg_layout = QVBoxLayout(self.prompt_group)
        self.prompt_tabs = PromptTabs(self._i18n)
        self.prompt_tabs.setMinimumHeight(150)
        pg_layout.addWidget(self.prompt_tabs)
        self._resize_handle = ResizeHandle(self.prompt_tabs, self._qsettings)
        pg_layout.addWidget(self._resize_handle)
        self.prompt_edit = self.prompt_tabs.prompt_edit
        self.negative_edit = self.prompt_tabs.negative_edit

        # 캐릭터 프롬프트 — 기본 프롬프트 바로 아래 (V5도 지원, 캡처의 characterPrompts)
        self.character_prompts = CharacterPromptsWidget(self._i18n)

        # 프롬프트 ↔ 캐릭터 프롬프트 사이를 드래그로 비율 조절 (V4 스플리터와 동일)
        self._prompt_char_splitter = QSplitter(Qt.Orientation.Vertical)
        self._prompt_char_splitter.setChildrenCollapsible(False)
        self._prompt_char_splitter.addWidget(self.prompt_group)
        self._prompt_char_splitter.addWidget(self.character_prompts)
        self._prompt_char_splitter.setHandleWidth(6)
        # 초기 비율 6:4 (프롬프트 : 캐릭터 프롬프트)
        self._prompt_char_splitter.setSizes([600, 400])
        left_layout.addWidget(self._prompt_char_splitter)

        # 모델 — 접힘 영역 밖 (모델을 바꾸면 샘플러·해상도 카탈로그가 전부 바뀐다)
        self.model_row = QWidget()
        model_layout = QHBoxLayout(self.model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        self.model_label = QLabel()
        self.model_combo = QComboBox()
        model_layout.addWidget(self.model_label)
        model_layout.addWidget(self.model_combo, stretch=1)
        left_layout.addWidget(self.model_row)

        # 해상도 — 접힘 상태와 무관하게 항상 편집 가능 (Req 10.1, 10.2)
        self.resolution_panel = ResolutionPanel(self._i18n)
        left_layout.addWidget(self.resolution_panel)

        # AI 설정 — 자주 바꾸지 않는 값은 접어 둔다 (Req 11.1)
        self.ai_settings_body = QWidget()
        form = QFormLayout(self.ai_settings_body)
        form.setContentsMargins(0, 0, 0, 0)
        self.sampler_combo = QComboBox()
        self.scheduler_combo = QComboBox()
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 150)
        self.cfg_spin = QDoubleSpinBox()
        self.cfg_spin.setRange(0.0, 30.0)
        self.cfg_spin.setSingleStep(0.1)
        # Prompt Guidance Rescale — 범위는 core/validation.py의 ParamSpec과 맞춘다
        self.rescale_spin = QDoubleSpinBox()
        self.rescale_spin.setRange(0.0, 1.0)
        self.rescale_spin.setSingleStep(0.05)
        self.rescale_spin.setDecimals(2)
        self.uc_preset_combo = QComboBox()
        self.quality_check = QCheckBox()
        self.quality_check.setChecked(True)

        seed_row = QHBoxLayout()
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("0")
        self.seed_random_check = QCheckBox()
        self.seed_random_check.setChecked(True)
        self.seed_random_check.toggled.connect(lambda on: self.seed_edit.setEnabled(not on))
        self.seed_edit.setEnabled(False)
        seed_row.addWidget(self.seed_edit)
        seed_row.addWidget(self.seed_random_check)

        self.sampler_label = QLabel()
        self.scheduler_label = QLabel()
        self.steps_label = QLabel()
        self.cfg_label = QLabel()
        self.rescale_label = QLabel()
        self.seed_label = QLabel()
        self.uc_preset_label = QLabel()
        form.addRow(self.sampler_label, self.sampler_combo)
        form.addRow(self.scheduler_label, self.scheduler_combo)
        form.addRow(self.steps_label, self.steps_spin)
        form.addRow(self.cfg_label, self.cfg_spin)
        form.addRow(self.rescale_label, self.rescale_spin)
        form.addRow(self.seed_label, seed_row)
        form.addRow(self.uc_preset_label, self.uc_preset_combo)
        form.addRow(self.quality_check)

        self.ai_section = CollapsibleSection(
            self._i18n, "image_options.title", summary_provider=self._compose_ai_summary
        )
        self.ai_section.set_content(self.ai_settings_body)
        left_layout.addWidget(self.ai_section)

        # i2i / 인페인팅 입력 (이미지 없으면 t2i)
        self.image_source = ImageSourceWidget(self._i18n)
        self.image_source.changed.connect(self._on_image_source_changed)
        self.image_source.setVisible(False)  # 보기 메뉴(F2)로 켠다
        left_layout.addWidget(self.image_source)

        self._wire_sections()

        # 스크롤하다 커서가 지나가는 것만으로 값이 바뀌지 않게 한다
        self._wheel_guard = guard_wheel(left)
        self.character_prompts.share_wheel_guard(self._wheel_guard)

        # 좌측 패널이 길어 세로 공간을 넘기므로 스크롤 영역으로 감싼다
        left_scroll = QScrollArea()
        left_scroll.setWidget(left)
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(470)

        # 생성 바는 스크롤 밖 맨 아래에 고정한다 (웹 UI처럼 상태바 바로 위).
        # 입력을 아무리 스크롤해도 생성 버튼은 늘 같은 자리에 있다.
        left_column = QWidget()
        column_layout = QVBoxLayout(left_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.addWidget(left_scroll, stretch=1)
        column_layout.addWidget(self._build_generate_bar())
        splitter.addWidget(left_column)

        # 오른쪽: 결과 미리보기 — 마우스 스크롤로 확대/축소된다 (ZoomableImageView)
        self.preview_label = ZoomableImageView()
        self.result_panel = self.preview_label
        splitter.addWidget(self.result_panel)
        splitter.setSizes(list(SPLITTER_SIZES))

        # 상태바 — V5 생성 크레딧 게이지 + Anlas 잔액
        self.status_label = QLabel()
        self.usage_label = QLabel()
        self.usage_bar = QProgressBar()
        self.usage_bar.setRange(0, 100)
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setFixedSize(110, 12)
        self.anlas_label = QLabel()
        self.login_label = QLabel()  # 로그인 상태 (V4의 label_loginstate 대응)

        # M3: CreditEstimator gauge — "X% (~N images)" display
        self._credit_gauge = StatusBarGauge(self._credit_estimator)
        self._credit_gauge.setVisible(False)

        self.statusBar().addWidget(self.status_label, stretch=1)
        self.statusBar().addPermanentWidget(self._credit_gauge)
        self.statusBar().addPermanentWidget(self.usage_label)
        self.statusBar().addPermanentWidget(self.usage_bar)
        self.statusBar().addPermanentWidget(self.anlas_label)
        self.statusBar().addPermanentWidget(self.login_label)
        self._show_usage(None)

        # 메뉴: 파일 / 보기 / 언어
        self.file_menu = self.menuBar().addMenu("")
        self.login_action = self.file_menu.addAction("")
        self.login_action.setShortcut("Ctrl+I")  # V4와 같은 단축키
        self.login_action.triggered.connect(self._on_open_login)
        # 계정을 여러 개 쓰는 사용자를 위한 토큰 전환 창 (최대 4개)
        self.accounts_action = self.file_menu.addAction("")
        self.accounts_action.setShortcut("Ctrl+Shift+I")
        self.accounts_action.triggered.connect(self._on_open_accounts)
        self.file_menu.addSeparator()
        self.image_info_action = self.file_menu.addAction("")
        self.image_info_action.triggered.connect(self._on_open_image_info)

        # 설정 파일 저장/불러오기 (V4의 Ctrl+S). V4는 불러오기가 Ctrl+L이었지만
        # V5에서 Ctrl+L은 로그 보기라 Ctrl+O를 쓴다.
        self.file_menu.addSeparator()
        self.save_settings_action = self.file_menu.addAction("")
        self.save_settings_action.setShortcut("Ctrl+S")
        self.save_settings_action.triggered.connect(self._on_save_settings_file)
        self.load_settings_action = self.file_menu.addAction("")
        self.load_settings_action.setShortcut("Ctrl+O")
        self.load_settings_action.triggered.connect(self._on_load_settings_file)

        # 설정 진입점은 여기 하나뿐이다 (Req 1.1)
        self.file_menu.addSeparator()
        self.options_action = self.file_menu.addAction("")
        self.options_action.setShortcut("Ctrl+,")
        self.options_action.triggered.connect(self.open_options)

        # 보기 (V4.5 참조) — 결과 패널 토글 / 레이아웃 초기화 / i2i 표시
        self.view_menu = self.menuBar().addMenu("")
        self.result_panel_action = self.view_menu.addAction("")
        self.result_panel_action.setCheckable(True)
        self.result_panel_action.setChecked(True)
        self.result_panel_action.setShortcut("F11")
        self.result_panel_action.toggled.connect(self._on_toggle_result_panel)

        self.reset_layout_action = self.view_menu.addAction("")
        self.reset_layout_action.setShortcut("Ctrl+R")
        self.reset_layout_action.triggered.connect(self.reset_layout)

        self.view_menu.addSeparator()
        # i2i는 자동 생성에서 잘 쓰지 않으므로 여기서 켤 때만 나타난다
        self.image_source_action = self.view_menu.addAction("")
        self.image_source_action.setCheckable(True)
        self.image_source_action.setShortcut("F2")
        self.image_source_action.toggled.connect(self.image_source.set_active)

        # M3: Gallery View action
        self.view_menu.addSeparator()
        self.gallery_action = self.view_menu.addAction("")
        self.gallery_action.setShortcut("F3")
        self.gallery_action.triggered.connect(self._on_open_gallery)

        # 도구 — 로그 보기 (강제 종료처럼 재현이 어려운 문제를 사후에 확인)
        self.tools_menu = self.menuBar().addMenu("")
        self.log_action = self.tools_menu.addAction("")
        self.log_action.setShortcut("Ctrl+L")
        self.log_action.triggered.connect(self.open_logs)

        # V5 크레딧 소모량 측정 — 켜면 매 장 후 잔량을 로그에 남긴다 (요청 1회 추가)
        self.measure_credit_action = self.tools_menu.addAction("")
        self.measure_credit_action.setCheckable(True)

        self.tools_menu.addSeparator()

        # M3: WD14 Auto-Tag action
        self.wd14_action = self.tools_menu.addAction("")
        self.wd14_action.setShortcut("Ctrl+T")
        self.wd14_action.triggered.connect(self._on_open_wd14)

        # M3: Presets action
        self.presets_action = self.tools_menu.addAction("")
        self.presets_action.setShortcut("Ctrl+P")
        self.presets_action.triggered.connect(self._on_open_presets)

        # 폴더 — 자주 여는 세 곳을 바로 연다 (V4와 같은 단축키: F5/F6/F7)
        self.folders_menu = self.menuBar().addMenu("")
        self.open_results_action = self.folders_menu.addAction("")
        self.open_results_action.setShortcut("F5")
        self.open_results_action.triggered.connect(lambda: self._open_folder("save_dir"))
        self.open_wildcards_action = self.folders_menu.addAction("")
        self.open_wildcards_action.setShortcut("F6")
        self.open_wildcards_action.triggered.connect(lambda: self._open_folder("wildcards_dir"))
        self.open_presets_action = self.folders_menu.addAction("")
        self.open_presets_action.setShortcut("F7")
        self.open_presets_action.triggered.connect(lambda: self._open_folder("presets_dir"))

        # 기타 — 새 버전 확인 (릴리스 zip으로 배포하므로 앱이 대신 알려 준다)
        self.etc_menu = self.menuBar().addMenu("")
        self.update_action = self.etc_menu.addAction("")
        self.update_action.triggered.connect(lambda: self.check_for_updates(manual=True))

        self.language_menu = self.menuBar().addMenu("")
        for code, name in self._i18n.get_available_languages().items():
            action = self.language_menu.addAction(name)
            action.triggered.connect(lambda _=False, c=code: self._set_language(c))

        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.resolution_panel.changed.connect(self._on_resolution_changed)  # Req 10.14

        # 프롬프트/캐릭터 프롬프트를 고치면 연속 생성 중 다음 이미지부터 반영한다
        self.prompt_edit.textChanged.connect(self._push_live_prompt)
        self.negative_edit.textChanged.connect(self._push_live_prompt)
        self.quality_check.toggled.connect(self._push_live_prompt)
        self.uc_preset_combo.currentIndexChanged.connect(self._push_live_prompt)
        self.character_prompts.prompts_changed.connect(self._push_live_prompt)

    def _build_generate_bar(self) -> QWidget:
        """매수·간격 + 생성 버튼 한 줄. 스크롤 밖에 고정되는 바."""
        self.generate_group = QGroupBox()
        bar_layout = QVBoxLayout(self.generate_group)
        batch_row = QHBoxLayout()
        quick_row = QHBoxLayout()
        button_row = QHBoxLayout()
        bar_layout.addLayout(batch_row)
        bar_layout.addLayout(quick_row)
        bar_layout.addLayout(button_row)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 99999)
        self.count_spin.setMaximumWidth(90)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 3600.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setMaximumWidth(90)
        self.count_label = QLabel()
        self.delay_label = QLabel()
        self.random_resolution_check = QCheckBox()

        self.once_button = QPushButton()
        self.auto_button = QPushButton()
        self.by_settings_button = QPushButton()  # V4의 "세팅별 연속 생성"
        self.stop_button = QPushButton()
        self.stop_button.setEnabled(False)
        self.once_button.clicked.connect(self._on_generate_once)
        self.auto_button.clicked.connect(self._on_generate_auto)
        self.by_settings_button.clicked.connect(self._on_generate_by_settings)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        batch_row.addWidget(self.count_label)
        batch_row.addWidget(self.count_spin)
        batch_row.addSpacing(12)
        batch_row.addWidget(self.delay_label)
        batch_row.addWidget(self.delay_spin)
        batch_row.addSpacing(12)
        batch_row.addWidget(self.random_resolution_check)
        batch_row.addStretch(1)

        # 퀵 매수 버튼 — 누르면 그 매수로 바로 연속 생성 (V4.5의 Quick Generation)
        self.quick_buttons: list[QPushButton] = []
        for index in range(QUICK_COUNT_SLOTS):
            button = QPushButton()
            button.clicked.connect(lambda _checked=False, i=index: self._on_quick_generate(i))
            quick_row.addWidget(button)
            self.quick_buttons.append(button)

        button_row.addWidget(self.once_button)
        button_row.addWidget(self.auto_button)
        button_row.addWidget(self.by_settings_button)
        button_row.addWidget(self.stop_button)
        # 스크롤 밖이라 휠 사고가 날 일은 없지만, 값 위젯 규칙은 똑같이 적용한다
        guard_wheel(self.generate_group, self._wheel_guard)
        return self.generate_group

    # ── M3 컴포넌트 초기화 ────────────────────────────────

    def _setup_m3_components(self) -> None:
        """M3 도구 패리티 위젯들을 메인 윈도우에 연결한다.

        _build_ui() 이후에 호출되어야 한다 (위젯이 존재해야 연결 가능).
        """
        # TagCompleter dropdown — 프롬프트/네거티브 + 캐릭터 슬롯 전부에 붙인다 (V4.5와 동일)
        self._completer_dropdowns: dict[QPlainTextEdit, TagCompleterDropdown] = {}
        self.character_prompts.slot_added.connect(self._attach_slot_completers)
        self.character_prompts.slot_removed.connect(self._detach_slot_completers)
        self.character_prompts.slot_added.connect(lambda _slot: self._apply_prompt_font())
        self._refresh_tag_completer()

        # Gallery View — save_dir 기반 썸네일 그리드
        self._gallery_view: GalleryView | None = None

    def _refresh_tag_completer(self) -> None:
        """설정된 경로(비어 있으면 내장 DB)로 태그 DB를 다시 읽고 드롭다운을 붙인다 (Req 7.3).

        옵션 저장 후에도 이 경로를 다시 타야 한다 — 전에 비활성이었다가 활성이 되는 경우
        (또는 그 반대)가 있어서 부착과 해제를 모두 처리해야 한다.
        """
        self._tag_completer = TagCompleter(resolve_database_path(self._settings.tag_database_path))
        self._tag_completer.load()
        for dropdown in list(self._completer_dropdowns.values()):
            dropdown.detach()
        self._completer_dropdowns = {}
        self._attach_completer(self.prompt_edit)
        self._attach_completer(self.negative_edit)
        for slot in self.character_prompts.slots:
            self._attach_slot_completers(slot)

    def _attach_completer(self, edit: QPlainTextEdit) -> None:
        """완성기가 활성이면 편집기 하나에 드롭다운을 붙인다."""
        if not self._tag_completer.is_enabled or not self._settings.tag_autocomplete_enabled:
            return
        self._completer_dropdowns[edit] = TagCompleterDropdown(edit, self._tag_completer)

    def _attach_slot_completers(self, slot: CharacterSlot) -> None:
        """새로 추가된 캐릭터 슬롯의 프롬프트/UC 입력창에도 자동완성을 붙인다."""
        self._attach_completer(slot.prompt_edit)
        self._attach_completer(slot.uc_edit)

    def _detach_slot_completers(self, slot: CharacterSlot) -> None:
        """슬롯이 사라지기 전에 드롭다운을 떼어 낸다 (삭제된 편집기 참조 방지)."""
        for edit in (slot.prompt_edit, slot.uc_edit):
            dropdown = self._completer_dropdowns.pop(edit, None)
            if dropdown is not None:
                dropdown.detach()

    @property
    def _prompt_completer(self) -> TagCompleterDropdown | None:
        """메인 프롬프트에 붙은 드롭다운 (없으면 None)."""
        return self._completer_dropdowns.get(self.prompt_edit)

    @property
    def _negative_completer(self) -> TagCompleterDropdown | None:
        """네거티브 입력창에 붙은 드롭다운 (없으면 None)."""
        return self._completer_dropdowns.get(self.negative_edit)

    # ── 폴더 열기 ────────────────────────────────────────

    def _open_folder(self, field: str) -> None:
        """설정의 경로 필드 하나를 OS 파일 탐색기로 연다 (F5/F6/F7).

        옵션 → 폴더에서 쓰는 것과 같은 헬퍼를 재사용한다 — 폴더가 없으면 만들고,
        열지 못하면 경고를 띄운다.
        """
        open_in_file_manager(getattr(self._settings, field), self._i18n.get_text, parent=self)

    # ── M3: Gallery View ─────────────────────────────────

    def _on_open_gallery(self) -> None:
        """Gallery 뷰를 별도 윈도우로 연다."""
        gallery_dir = str(self._settings.gallery_dir_path())
        if self._gallery_view is None:
            self._gallery_view = GalleryView(
                save_dir=gallery_dir,
                i18n=self._i18n,
                parent=self,
            )
            self._gallery_view.reuse_requested.connect(self._on_gallery_reuse)
            self._gallery_view.setWindowTitle(self._i18n.get_text("menu.gallery_view"))
            self._gallery_view.setMinimumSize(600, 400)

        # 옵션에서 폴더를 바꿨을 수 있다 — 열 때마다 설정을 따라간다.
        # (창 안에서 고른 폴더는 설정을 덮어쓰지 않으므로 여기서 되돌아온다.)
        self._gallery_view.set_directory(gallery_dir)
        self._gallery_view.show()
        self._gallery_view.raise_()

    def _on_gallery_reuse(self, path: str) -> None:
        """Gallery에서 Reuse Settings 요청 시 기존 apply_reusable로 위임."""
        self.open_image_info(path)

    # ── M3: WD14 Auto-Tag ────────────────────────────────

    def _on_open_wd14(self) -> None:
        """WD14 Auto-Tag 다이얼로그를 연다.

        모델과 태그 CSV는 옵션 → 태그에서 지정한 폴더(`wd14_dir`)에서, 거기서 고른
        모델(`wd14_model`)을 우선해 찾는다 (기본 폴더는 데이터 폴더의 `wd14/`).
        파일 이름은 받은 곳마다 다르므로 폴더 안을 훑는다 —
        `core.wd14_tagger.resolve_model_files` 참고.
        """
        from ..core.wd14_tagger import resolve_model_files, runtime_error

        tr = self._i18n.get_text

        # onnxruntime을 쓸 수 없으면 창을 열어 봐야 아무것도 못 한다 — 이유를 그대로 알린다.
        # (모델이 없는 것과 원인이 전혀 다르므로 안내도 따로 한다.)
        failure = runtime_error()
        if failure:
            logger.warning("WD14 runtime unavailable: %s", failure)
            QMessageBox.information(
                self, tr("menu.wd14_auto_tag"), tr("errors.wd14_runtime_missing", failure)
            )
            return

        from .wd14_dialog import WD14Dialog

        directory = self._settings.wd14_dir_path()
        model_path, tags_path = resolve_model_files(directory, self._settings.wd14_model)

        if model_path is None or tags_path is None:
            missing = "*.onnx" if model_path is None else "*.csv"
            logger.warning("WD14 model files not found in %s (missing %s)", directory, missing)
            QMessageBox.information(
                self,
                tr("menu.wd14_auto_tag"),
                tr("errors.wd14_model_missing", missing, str(directory)),
            )
            return

        try:
            from ..core.wd14_tagger import WD14Tagger

            tagger = WD14Tagger(model_path=model_path, tags_path=tags_path)
        except Exception:
            logger.warning("WD14 tagger could not be initialized")
            QMessageBox.information(
                self,
                tr("menu.wd14_auto_tag"),
                tr("errors.wd14_model_missing", "*.onnx", str(directory)),
            )
            return

        dialog = WD14Dialog(tagger=tagger, i18n=self._i18n, parent=self)
        dialog.tags_selected.connect(self._on_wd14_tags_selected)
        dialog.exec()

    def _on_wd14_tags_selected(self, tags: list[str]) -> None:
        """WD14에서 선택된 태그를 현재 포커스된 프롬프트 필드에 추가."""
        from ..core.wd14_tagger import append_tags_to_prompt

        # 현재 포커스된 프롬프트 필드 결정 (기본: main prompt)
        target = self.prompt_edit
        if self.negative_edit.hasFocus():
            target = self.negative_edit

        current = target.toPlainText()
        target.setPlainText(append_tags_to_prompt(current, tags))

    # ── 설정 파일 저장 / 불러오기 (V4 방식) ────────────────

    _SETTINGS_FILE_FILTER = "설정 파일 (*.json *.txt);;모든 파일 (*)"

    def _on_save_settings_file(self) -> None:
        """현재 생성 설정을 파일 하나로 저장한다 (Ctrl+S)."""
        tr = self._i18n.get_text
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("menu.save_settings"),
            str(Path(self._settings.presets_dir) / "settings.json"),
            self._SETTINGS_FILE_FILTER,
        )
        if not path:
            return
        seed = None if self.seed_random_check.isChecked() else self._seed_value()
        try:
            settings_file.save(Path(path), self._get_current_preset_config(), seed)
        except OSError as e:
            QMessageBox.warning(self, tr("errors.title"), f"{tr('menu.save_settings')}\n\n{e}")
            return
        self.status_label.setText(tr("menu.save_settings"))

    def _on_load_settings_file(self) -> None:
        """파일에서 생성 설정을 불러온다 (Ctrl+O). V4가 저장한 .txt도 읽는다."""
        tr = self._i18n.get_text
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("menu.load_settings"),
            self._settings.presets_dir,
            self._SETTINGS_FILE_FILTER,
        )
        if not path:
            return

        # 파일에 없는 항목은 지금 화면 값을 그대로 둔다 — V4 파일은 버전마다 담는 항목이
        # 달라서(오래된 것은 `model`조차 없다) 모델 기본값만으로는 필수 항목이 빈다.
        defaults = self._get_current_preset_config().model_dump()
        try:
            loaded = settings_file.load(Path(path), defaults=defaults)
        except PresetError as e:
            QMessageBox.warning(self, tr("errors.title"), str(e))
            return

        self._on_preset_loaded(loaded.preset)
        if loaded.seed is not None:
            self.seed_random_check.setChecked(False)
            self.seed_edit.setText(str(loaded.seed))

    # ── M3: Presets ──────────────────────────────────────

    def _on_open_presets(self) -> None:
        """Preset Manager 다이얼로그를 연다."""
        dialog = PresetManagerDialog(
            store=self._preset_store,
            get_current_config=self._get_current_preset_config,
            parent=self,
        )
        dialog.preset_loaded.connect(self._on_preset_loaded)
        dialog.exec()

    def _get_current_preset_config(self) -> GenerationPreset:
        """현재 UI 상태를 GenerationPreset으로 변환 (프리셋 저장용)."""
        spec = self.current_spec()
        size = self.resolution_panel.size()
        return GenerationPreset(
            name="",  # 저장 시 입력받음
            model=spec.key,
            width=size[0],
            height=size[1],
            steps=self.steps_spin.value(),
            cfg_scale=self.cfg_spin.value(),
            cfg_rescale=self.rescale_spin.value(),
            sampler=self.sampler_combo.currentText(),
            scheduler=self.scheduler_combo.currentText(),
            quality_tags=self.quality_check.isChecked(),
            uc_preset=self.uc_preset_combo.currentData() or "heavy",
            prompt=self.prompt_edit.toPlainText(),
            negative_prompt=self.negative_edit.toPlainText(),
            characters=[
                CharacterPromptPreset(prompt=c.prompt, uc=c.uc, center_x=c.center_x, center_y=c.center_y)
                for c in self.character_prompts.captions()
            ],
            use_coords=self.character_prompts.use_coords(),
            manual_position_override=self.character_prompts.manual_position_override(),
        )

    def _on_preset_loaded(self, preset: GenerationPreset) -> None:
        """프리셋을 UI에 원자적으로 적용한다.

        model이 빈 문자열이면 알 수 없는 모델 — 모델 셀렉터는 변경하지 않는다.
        """
        # Model (빈 문자열 = 알 수 없는 모델, 스킵)
        if preset.model:
            idx = self.model_combo.findData(preset.model)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)

        # Resolution
        self._select_resolution(preset.width, preset.height)

        # Sampler / scheduler
        sampler_idx = self.sampler_combo.findText(preset.sampler)
        if sampler_idx >= 0:
            self.sampler_combo.setCurrentIndex(sampler_idx)
        scheduler_idx = self.scheduler_combo.findText(preset.scheduler)
        if scheduler_idx >= 0:
            self.scheduler_combo.setCurrentIndex(scheduler_idx)

        # Numeric fields
        self.steps_spin.setValue(preset.steps)
        self.cfg_spin.setValue(preset.cfg_scale)
        self.rescale_spin.setValue(preset.cfg_rescale)

        # UC preset / quality tags
        uc_idx = self.uc_preset_combo.findData(preset.uc_preset)
        if uc_idx >= 0:
            self.uc_preset_combo.setCurrentIndex(uc_idx)
        self.quality_check.setChecked(preset.quality_tags)

        # Prompts
        if preset.prompt:
            self.prompt_edit.setPlainText(preset.prompt)
        if preset.negative_prompt:
            self.negative_edit.setPlainText(preset.negative_prompt)

        # Characters (Bug fix: 프리셋에 캐릭터 프롬프트가 없으면 기존 캐릭터도 정리한다 — 원자적 적용)
        self.character_prompts.load_captions(
            tuple(
                CharacterCaption(prompt=c.prompt, uc=c.uc, center_x=c.center_x, center_y=c.center_y)
                for c in preset.characters
            )
        )
        self.character_prompts.set_use_coords(preset.use_coords)
        self.character_prompts.set_manual_position_override(preset.manual_position_override)

        self.status_label.setText(self._i18n.get_text("menu.preset_applied"))

    # ── M3: Credit Gauge 업데이트 ────────────────────────

    def _update_credit_gauge(self, current_percent: int) -> None:
        """크레딧 게이지를 현재 해상도/스텝 기준으로 갱신."""
        size = self.target_size()
        steps = self.steps_spin.value()
        self._credit_gauge.update_display(current_percent, size[0], size[1], steps)
        self._credit_gauge.setVisible(True)

    # ── ModelSpec → 콤보 ─────────────────────────────────

    def _populate_models(self) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for key, spec in MODEL_REGISTRY.items():
            if not spec.ui_visible:  # V4 계열은 레지스트리에만 남기고 UI에서는 감춘다
                continue
            label = spec.api_name + (" ⚠" if spec.incomplete else "")
            self.model_combo.addItem(label, userData=key)
        self.model_combo.blockSignals(False)

    def current_spec(self) -> ModelSpec:
        return get_spec(self.model_combo.currentData())

    def _select_resolution(self, width: int, height: int) -> bool:
        """해상도 패널에 (w, h)를 적용한다.

        반환값은 적용된 크기가 선택 가능 목록에 있는지 — 없으면 패널이 `직접 입력`으로
        표시한다 (Req 10.10). 크기 자체는 어느 경우에도 그대로 적용된다.
        """
        self.resolution_panel.set_size(width, height)
        return self.resolution_panel.current_group() is not None

    def _rebuild_resolution_catalog(self) -> None:
        """모델 스펙 + 옵션 설정으로 선택 가능 해상도를 다시 만든다 (Req 5.9, 5.10)."""
        catalog = ResolutionCatalog.from_settings(self.current_spec(), self._settings)
        self.resolution_panel.set_catalog(catalog)

    # ── 레이아웃 (보기 메뉴) ──────────────────────────────

    def _on_toggle_result_panel(self, visible: bool) -> None:
        """결과 패널을 접어 입력 패널을 넓게 쓴다."""
        self.result_panel.setVisible(visible)
        if visible and self._splitter.sizes()[1] == 0:
            self._splitter.setSizes(list(SPLITTER_SIZES))

    def reset_layout(self) -> None:
        """창 크기와 분할 비율을 기본값으로 되돌린다 (패널을 잃어버렸을 때 복구용).

        모든 접이식 섹션도 접힘 상태로 되돌린다 (Req 12.3) — `toggled`를 타고
        `settings.ui`까지 반영된다.
        """
        self.result_panel_action.setChecked(True)
        self.result_panel.setVisible(True)
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self.resize(*WINDOW_SIZE)
        self._splitter.setSizes(list(SPLITTER_SIZES))
        self.ai_section.set_expanded(False)

    def target_size(self) -> tuple[int, int]:
        """실제로 생성될 크기 — i2i/인페인팅이면 원본 이미지 크기를 따른다."""
        if self.image_source.size is not None:
            return self.image_source.size
        return self.resolution_panel.size()

    def _sync_position_aspect(self) -> None:
        """캐릭터 위치 캔버스를 생성 해상도 비율로 맞춘다."""
        self.character_prompts.set_aspect(*self.target_size())

    # ── 스플리터 상태 저장·복원 (프롬프트/캐릭터 + 좌·우 메인 스플리터) ─────

    _SPLITTER_KEY = "ui/prompt_char_splitter"
    _MAIN_SPLITTER_KEY = "ui/main_splitter"
    _GEOMETRY_KEY = "ui/main_window_geometry"

    def _save_splitters(self) -> None:
        """스플리터 상태를 QSettings에 저장한다."""
        self._qsettings.setValue(self._SPLITTER_KEY, self._prompt_char_splitter.saveState())
        self._qsettings.setValue(self._MAIN_SPLITTER_KEY, self._splitter.saveState())

    def _restore_splitters(self) -> None:
        """QSettings에서 스플리터 상태를 복원한다."""
        state = self._qsettings.value(self._SPLITTER_KEY)
        if state is not None:
            self._prompt_char_splitter.restoreState(state)
        main_state = self._qsettings.value(self._MAIN_SPLITTER_KEY)
        if main_state is not None:
            self._splitter.restoreState(main_state)

    def _save_window_geometry(self) -> None:
        """창 크기·위치를 QSettings에 저장한다."""
        self._qsettings.setValue(self._GEOMETRY_KEY, self.saveGeometry())

    def _restore_window_geometry(self) -> bool:
        """QSettings에 저장된 크기·위치가 있으면 복원한다. 복원했으면 True."""
        state = self._qsettings.value(self._GEOMETRY_KEY)
        if state is None:
            return False
        return bool(self.restoreGeometry(state))

    def _on_resolution_changed(self) -> None:
        """해상도가 바뀌면 캐릭터 위치 캔버스 비율을 다시 맞춘다 (Req 10.14).

        크레딧 경고 표시는 패널이 스스로 처리한다.
        """
        self._sync_position_aspect()
        self._push_live_resolution()

    def _on_image_source_changed(self) -> None:
        """i2i / 인페인팅 원본이 있으면 해상도 입력을 잠근다 (Req 10.13).

        생성 크기는 원본 이미지 크기를 따르므로(`target_size`), 패널이 그 사실을 문구로
        알리고 입력란을 비활성화한다.
        """
        size = self.image_source.size
        self.resolution_panel.set_source_locked(size is not None, size)
        self._sync_position_aspect()
        self._push_live_resolution()

    def _push_live_resolution(self) -> None:
        """배치 진행 중이면 해상도 변경을 다음 이미지부터 반영한다 (진행 중인 이미지는 그대로).

        i2i/인페인팅으로 잠겨 있으면 랜덤 후보를 만들지 않는다 (build_job과 동일한 규칙).
        """
        if not self._service.is_running:
            return
        size = self.target_size()
        choices = () if self.image_source.size is not None else self.resolution_panel.aspect_random_choices()
        self._service.set_live_resolution(size[0], size[1], choices)

    def _push_live_prompt(self) -> None:
        """배치 진행 중이면 프롬프트/캐릭터 프롬프트 변경을 다음 이미지부터 반영한다.

        진행 중인 이미지에는 영향이 없다 (`build_job`과 같은 조합 규칙을 그대로 따른다).
        """
        if not self._service.is_running:
            return
        spec = self.current_spec()
        prompt = self.prompt_edit.toPlainText().strip()
        if self.quality_check.isChecked():
            prompt += spec.quality_tags
        uc_key = self.uc_preset_combo.currentData() or "none"
        preset_uc = spec.uc_presets.get(uc_key, "")
        user_uc = self.negative_edit.toPlainText().strip()
        negative = ", ".join(part for part in (preset_uc, user_uc) if part)
        self._service.set_live_prompt(
            prompt, negative, self.character_prompts.captions(), self.character_prompts.use_coords()
        )

    def _on_model_changed(self) -> None:
        spec = self.current_spec()
        for combo, values in (
            (self.sampler_combo, spec.samplers),
            (self.scheduler_combo, spec.schedulers),
        ):
            current = combo.currentText()
            combo.clear()
            combo.addItems(list(values))
            if current in values:
                combo.setCurrentText(current)
        self._rebuild_resolution_catalog()
        self.uc_preset_combo.clear()
        for preset_key in spec.uc_presets:
            self.uc_preset_combo.addItem(preset_key, userData=preset_key)
        # 모델이 지원하지 않는 기능은 UI가 선제적으로 막는다 (엔진 검증은 최후 방어선)
        self.character_prompts.setEnabled("characters" in spec.supports)
        supports_i2i = "img2img" in spec.supports
        self.image_source.setEnabled(supports_i2i)
        self.image_source_action.setEnabled(supports_i2i)
        if not supports_i2i:
            self.image_source_action.setChecked(False)
        self.image_source.draw_mask_button.setEnabled(
            "inpaint" in spec.supports and self.image_source.image_bytes is not None
        )
        defaults = spec.defaults
        self.sampler_combo.setCurrentText(str(defaults.get("sampler", "")))
        self.scheduler_combo.setCurrentText(str(defaults.get("scheduler", "")))
        self.steps_spin.setValue(int(defaults.get("steps", 28)))
        self.cfg_spin.setValue(float(defaults.get("cfg_scale", 5.0)))
        self.rescale_spin.setValue(float(defaults.get("cfg_rescale", 0.0)))

    # ── 설정 적용/수집 ────────────────────────────────────

    def _apply_settings(self) -> None:
        g = self._settings.generation
        idx = self.model_combo.findData(g.model)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_model_changed()
        self._select_resolution(g.width, g.height)
        if g.sampler in self.current_spec().samplers:
            self.sampler_combo.setCurrentText(g.sampler)
        self.steps_spin.setValue(g.steps)
        self.cfg_spin.setValue(g.cfg_scale)
        self.rescale_spin.setValue(g.cfg_rescale)
        self.quality_check.setChecked(g.quality_tags)
        uc_idx = self.uc_preset_combo.findData(g.uc_preset)
        if uc_idx >= 0:
            self.uc_preset_combo.setCurrentIndex(uc_idx)
        # 시드는 시작할 때 항상 랜덤. 고정 시드는 의도적으로 켜는 일회성 상태다
        # (연속 생성에서 같은 그림만 반복되는 사고를 막는다).
        self.seed_random_check.setChecked(True)
        if g.seed >= 0:
            self.seed_edit.setText(str(g.seed))
        self.count_spin.setValue(self._settings.batch.count)
        self.delay_spin.setValue(self._settings.batch.delay_seconds)
        self.random_resolution_check.setChecked(self._settings.batch.random_resolution)
        self._refresh_quick_buttons()
        self.image_source_action.setChecked(
            self._settings.show_image_source and self.image_source_action.isEnabled()
        )
        self.measure_credit_action.setChecked(self._settings.measure_credit)
        self._apply_prompts()
        self._apply_prompt_font()
        self._sync_position_aspect()
        self._apply_section_states()  # Req 12.2
        self._refresh_section_summaries()  # Req 11.6, 11.8

    def _apply_prompt_font(self) -> None:
        """설정된 폰트 크기·색상을 프롬프트·네거티브·캐릭터 프롬프트 입력란에 적용한다.

        크기 0 / 색상 ""이면 해당 속성을 스타일시트에서 뺀다 — 테마 기본값이 그대로 보인다.
        강조(`2::text::`)/약화(`-2::text::`) 색은 위젯 스타일시트가 아니라 각 `PromptTabs`가
        들고 있는 `PromptHighlighter`에 적용한다 (V4의 high_emphasis_color/low_emphasis_color).
        """
        f = self._settings.prompt_font
        parts = []
        if f.size > 0:
            parts.append(f"font-size: {f.size}pt;")
        if f.color:
            parts.append(f"color: {f.color};")
        qss = " ".join(parts)
        edits = [self.prompt_edit, self.negative_edit]
        for slot in self.character_prompts.slots:
            edits.extend((slot.prompt_edit, slot.uc_edit))
        for edit in edits:
            edit.setStyleSheet(qss)

        high_color = QColor(f.emphasis_color) if f.emphasis_color else None
        low_color = QColor(f.deemphasis_color) if f.deemphasis_color else None
        self.prompt_tabs.set_emphasis_colors(high_color, low_color)
        for slot in self.character_prompts.slots:
            slot.tabs.set_emphasis_colors(high_color, low_color)

    def _apply_prompts(self) -> None:
        """마지막 세션의 프롬프트 복원."""
        p = self._settings.prompts
        self.prompt_edit.setPlainText(p.prompt)
        self.negative_edit.setPlainText(p.negative_prompt)
        if p.characters:
            self.character_prompts.load_captions(
                tuple(
                    CharacterCaption(prompt=c.prompt, uc=c.uc, center_x=c.center_x, center_y=c.center_y)
                    for c in p.characters
                )
            )
        self.character_prompts.set_use_coords(p.use_coords)
        self.character_prompts.set_manual_position_override(p.manual_position_override)

    def collect_settings(self) -> AppSettings:
        """현재 위젯 상태를 설정 객체로 (종료 시 영속화용)."""
        self._save_splitters()
        self._save_window_geometry()
        s = self._settings
        g = s.generation
        g.model = self.model_combo.currentData()
        g.width, g.height = self.resolution_panel.size()
        g.sampler = self.sampler_combo.currentText()
        g.scheduler = self.scheduler_combo.currentText()
        g.steps = self.steps_spin.value()
        g.cfg_scale = self.cfg_spin.value()
        g.cfg_rescale = self.rescale_spin.value()
        g.quality_tags = self.quality_check.isChecked()
        g.uc_preset = self.uc_preset_combo.currentData() or "heavy"
        g.seed = -1 if self.seed_random_check.isChecked() else self._seed_value()
        s.batch.count = self.count_spin.value()
        s.batch.delay_seconds = self.delay_spin.value()
        s.batch.random_resolution = self.random_resolution_check.isChecked()
        s.show_image_source = self.image_source_action.isChecked()
        s.measure_credit = self.measure_credit_action.isChecked()
        p = s.prompts
        p.prompt = self.prompt_edit.toPlainText()
        p.negative_prompt = self.negative_edit.toPlainText()
        p.use_coords = self.character_prompts.use_coords()
        p.manual_position_override = self.character_prompts.manual_position_override()
        p.characters = [
            CharacterPromptState(prompt=c.prompt, uc=c.uc, center_x=c.center_x, center_y=c.center_y)
            for c in self.character_prompts.captions()
        ]
        return s

    def _refresh_quick_buttons(self) -> None:
        """설정의 퀵 매수 값으로 버튼 문구를 다시 만든다 (옵션 저장·언어 전환 후)."""
        tr = self._i18n.get_text
        counts = self._settings.batch.quick_counts
        hint = tr("generate_dialog.preset_group")
        for index, button in enumerate(self.quick_buttons):
            has_value = index < len(counts)
            button.setText(tr("generate_dialog.count_n", counts[index]) if has_value else "")
            button.setToolTip(hint if has_value else "")
            button.setVisible(has_value)
            button.setEnabled(has_value and not self._is_running and self._logged_in)

    def _seed_value(self) -> int:
        try:
            return max(0, int(self.seed_edit.text().strip() or "0"))
        except ValueError:
            return 0

    # ── 옵션 ─────────────────────────────────────────────

    def open_options(self) -> OptionsDialog:
        """옵션 다이얼로그를 모달로 연다 (Req 1.1).

        다이얼로그는 드래프트 사본을 편집하므로, 저장에 성공했을 때만 `applied`가 오고
        그때 `_on_options_applied()`가 화면을 맞춘다. 취소는 아무것도 하지 않는다.
        """
        dialog = OptionsDialog(
            self._i18n,
            self._settings,
            supports_i2i="img2img" in self.current_spec().supports,
            parent=self,
        )
        dialog.applied.connect(self._on_options_applied)
        dialog.exec()
        if dialog.result() == QDialog.DialogCode.Accepted:
            # 안내는 다이얼로그가 닫힌 뒤에 — 저장 중에 모달을 겹쳐 띄우지 않는다 (Req 2.6).
            self._show_option_notices(dialog.notices())
        return dialog

    def _on_options_applied(self) -> None:
        """저장된 옵션을 메인 윈도우 위젯·메뉴·컴포넌트에 반영한다."""
        s = self._settings
        self.count_spin.setValue(s.batch.count)  # Req 4.2
        self.delay_spin.setValue(s.batch.delay_seconds)
        self._refresh_quick_buttons()
        self.image_source_action.setChecked(  # Req 6.3 (토글이 패널 표시까지 맞춘다)
            s.show_image_source and self.image_source_action.isEnabled()
        )
        self.measure_credit_action.setChecked(s.measure_credit)  # Req 8.8
        self._refresh_tag_completer()  # Req 7.3
        self._apply_prompt_font()
        ensure_dirs(s)  # 새로 지정한 폴더를 미리 만들어 둔다
        self._preset_store = PresetStore(Path(s.presets_dir))  # Req 2.5
        self._service.reload_artist_combos(s.artist_combos_dir)
        self._rebuild_resolution_catalog()  # Req 5.9, 5.10
        self._apply_section_states()  # Req 6.4 (섹션 접힘 상태 초기화)
        self._refresh_section_summaries()

    # ── 접이식 섹션 (요약 / 접힘 상태) ──────────────────────

    def _wire_sections(self) -> None:
        """본문 값 변경 → 요약 갱신, 접힘 상태 변경 → 설정 반영 (Req 11.8, 12.1)."""
        for signal in (
            self.steps_spin.valueChanged,
            self.cfg_spin.valueChanged,
            self.rescale_spin.valueChanged,
            self.sampler_combo.currentTextChanged,
            self.seed_random_check.toggled,
            self.seed_edit.textChanged,
            self.uc_preset_combo.currentTextChanged,
            self.quality_check.toggled,
        ):
            signal.connect(self._refresh_section_summaries)
        self.ai_section.toggled.connect(lambda on: self._on_section_toggled("ai_settings_expanded", on))

    def _compose_ai_summary(self) -> str:
        """접힌 `AI 설정` 섹션에 보일 한 줄 요약 (Req 11.6)."""
        tr = self._i18n.get_text
        seed_label = (
            tr("image_options.random") if self.seed_random_check.isChecked() else str(self._seed_value())
        )
        return compose_ai_summary(
            steps=self.steps_spin.value(),
            cfg_scale=self.cfg_spin.value(),
            seed_label=seed_label,
            sampler=self.sampler_combo.currentText(),
            template=tr("ui.summary_ai"),
        )

    def _refresh_section_summaries(self) -> None:
        """`AI 설정` 요약을 현재 값으로 다시 만든다 (접힘 여부와 무관, Req 11.8)."""
        self.ai_section.refresh_summary()

    def _on_section_toggled(self, field: str, expanded: bool) -> None:
        """섹션 접힘 상태를 `settings.ui`에 반영한다 (Req 12.1)."""
        setattr(self._settings.ui, field, expanded)

    def _apply_section_states(self) -> None:
        """`settings.ui`에 저장된 펼침 상태를 섹션에 적용한다 (Req 12.2)."""
        self.ai_section.set_expanded(self._settings.ui.ai_settings_expanded)

    def _show_option_notices(self, keys: tuple[str, ...]) -> None:
        """옵션 페이지가 낸 안내 문구를 한 번에 보여 준다 (Req 2.6)."""
        if not keys:
            return
        tr = self._i18n.get_text
        QMessageBox.information(self, tr("options.title"), "\n".join(tr(key) for key in keys))

    # ── 로그 ─────────────────────────────────────────────

    def open_logs(self) -> LogDialog:
        """로그 뷰어. 닫을 때 디버그 로그 설정을 즉시 반영한다."""
        directory = self._settings.log_dir_path()
        dialog = LogDialog(
            self._i18n,
            log_path(directory),
            crash_log_path(directory),
            debug_enabled=self._settings.debug_logging,
            parent=self,
        )
        dialog.exec()
        if dialog.debug_enabled != self._settings.debug_logging:
            self._settings.debug_logging = dialog.debug_enabled
            configure_logging(directory, debug=dialog.debug_enabled)
            logger.info("debug logging %s", "enabled" if dialog.debug_enabled else "disabled")
        return dialog

    # ── 이미지 정보 (드래그&드롭 / 메뉴) ────────────────────

    # ── 로그인 / 로그아웃 (V4식) ──────────────────────────

    def _validate_token(self, token: str) -> None:
        """토큰으로 로그인하고 실제 API를 한 번 호출해 유효성을 확인한다.

        `login_with_token`은 형식만 보므로, 실제 호출이 실패하면 세션에 못 쓰는 토큰이
        남는다. 그 상태로 두면 `is_logged_in`이 참이 되어 로그인된 것처럼 보인다 —
        실패하면 반드시 되돌린다. `app.main()`의 시작 시 자동 로그인도 같은 절차를 쓴다.
        """
        session = self._client.session
        session.login_with_token(token)
        try:
            self._client.get_anlas()
        except Exception:
            session.logout()
            raise

    def _on_open_login(self) -> None:
        """파일 → 로그인. 로그인 상태면 로그아웃 창을, 아니면 토큰 입력 창을 띄운다."""
        session = self._client.session
        dialog = LoginDialog(
            self._i18n,
            self._validate_token,
            parent=self,
            logged_in=session.is_logged_in,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if dialog.logout_requested:
            self._logout()
            return

        # 로그인 성공 (다이얼로그가 이미 검증했다)
        if dialog.remember:
            credentials.save_credential(credentials.TOKEN_KEY, dialog.token)
        else:
            credentials.delete_credential(credentials.TOKEN_KEY)
        self.set_logged_in(True)

    def _logout(self) -> None:
        """세션과 저장된 토큰을 함께 지운다 — 다음 실행 때 다시 물어본다."""
        tr = self._i18n.get_text
        self._client.session.logout()
        credentials.delete_credential(credentials.TOKEN_KEY)
        self.set_logged_in(False)
        QMessageBox.information(self, tr("dialogs.logout_complete_title"), tr("dialogs.logout_complete"))

    # ── API 계정 전환 (여러 계정을 번갈아 쓰는 사용자용) ──────

    def _on_open_accounts(self) -> None:
        """파일 → API 계정 관리. 저장해 둔 토큰 중 하나로 즉시 갈아탄다."""
        dialog = AccountsDialog(
            self._i18n,
            self._switch_account,
            current_token=self._client.session.access_token or "",
            parent=self,
        )
        dialog.exec()
        if dialog.switched:
            self.set_logged_in(self._client.session.is_logged_in)

    def _switch_account(self, token: str) -> None:
        """새 토큰으로 로그인해 본다. 실패하면 쓰던 계정을 그대로 되돌린다.

        `_validate_token`은 실패할 때 세션을 비우므로, 되돌리지 않으면 멀쩡히 쓰던
        계정까지 로그아웃된 것처럼 보인다. 진행 중인 연속 생성은 건드리지 않는다 —
        다음 요청부터 새 토큰이 쓰인다.
        """
        previous = self._client.session.access_token
        try:
            self._validate_token(token)
        except Exception:
            if previous and accounts.is_valid_token(previous):
                self._client.session.login_with_token(previous)
            raise
        logger.info("switched to another API account")

    def set_logged_in(self, logged_in: bool) -> None:
        """로그인 상태를 반영한다 — 생성 버튼, 상태바, 잔액 표시.

        창을 띄운 쪽(`app.main()`)도 시작 직후 이걸 한 번 부른다.
        """
        tr = self._i18n.get_text
        self._logged_in = logged_in
        self._refresh_generate_buttons()
        self.login_label.setText(tr("statusbar.logged_in") if logged_in else tr("statusbar.before_login"))
        if logged_in:
            self.refresh_anlas()
        else:
            # 남은 잔액 표시가 로그아웃 후에도 떠 있으면 오해를 준다
            self.anlas_label.clear()
            self._show_usage(None)
            self._credit_gauge.setVisible(False)
            self.status_label.setText(tr("statusbar.before_login"))

    def _on_open_image_info(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._i18n.get_text("image_info.open"),
            self._settings.save_dir,
            "Images (*.png *.webp)",
        )
        if path:
            self.open_image_info(path)

    def open_image_info(self, path: str) -> ImageInfoDialog:
        """PNG의 생성 정보를 보여주고, 사용자가 수락하면 설정을 UI에 적용한다."""
        from pathlib import Path as _Path

        dialog = ImageInfoDialog(self._i18n, self)
        dialog.settings_selected.connect(self.apply_reusable)
        dialog.load_file(_Path(path))
        dialog.exec()
        return dialog

    def apply_reusable(self, s: ReusableSettings) -> None:
        """메타데이터에서 복원한 값만 위젯에 반영한다 (None인 필드는 현재 값 유지)."""
        if s.prompt is not None:
            self.prompt_edit.setPlainText(s.prompt)
        if s.negative_prompt is not None:
            # 프리셋 텍스트가 앞에 붙어 있으므로, 재사용 시에는 프리셋을 끄고 원문 그대로 쓴다
            none_index = self.uc_preset_combo.findData("none")
            if none_index >= 0:
                self.uc_preset_combo.setCurrentIndex(none_index)
            self.negative_edit.setPlainText(s.negative_prompt)
            self.quality_check.setChecked(False)  # 프롬프트에 이미 포함되어 있음
        if s.seed is not None:
            self.seed_random_check.setChecked(False)
            self.seed_edit.setText(str(s.seed))
        if s.steps is not None:
            self.steps_spin.setValue(s.steps)
        if s.cfg_scale is not None:
            self.cfg_spin.setValue(s.cfg_scale)
        if s.cfg_rescale is not None:
            self.rescale_spin.setValue(s.cfg_rescale)
        if s.sampler is not None:
            index = self.sampler_combo.findText(s.sampler)
            if index >= 0:
                self.sampler_combo.setCurrentIndex(index)
        if s.scheduler is not None:
            index = self.scheduler_combo.findText(s.scheduler)
            if index >= 0:
                self.scheduler_combo.setCurrentIndex(index)
        if s.width and s.height:
            self._select_resolution(s.width, s.height)
        if s.characters:
            self.character_prompts.load_captions(s.characters)
        self.status_label.setText(self._i18n.get_text("image_info.applied"))

    # ── 드래그&드롭 ─────────────────────────────────────

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if self._dropped_image(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        path = self._dropped_image(event)
        if path is not None:
            event.acceptProposedAction()
            self.open_image_info(path)

    @staticmethod
    def _dropped_image(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if Path(local).suffix.lower() in (".png", ".webp"):
                return local
        return None

    # ── 잡 빌드/실행 ─────────────────────────────────────

    def build_job(self, count: int) -> GenerationJob:
        """위젯 상태를 불변 잡으로 스냅숏. 여기 이후 워커는 위젯을 안 본다."""
        spec = self.current_spec()
        prompt = self.prompt_edit.toPlainText().strip()
        if self.quality_check.isChecked():
            prompt += spec.quality_tags
        uc_key = self.uc_preset_combo.currentData() or "none"
        preset_uc = spec.uc_presets.get(uc_key, "")
        user_uc = self.negative_edit.toPlainText().strip()
        negative = ", ".join(part for part in (preset_uc, user_uc) if part)

        # i2i/인페인팅은 원본 이미지 크기를 그대로 쓴다 (스모크로 검증된 동작)
        size = self.target_size()
        randomize = self.seed_random_check.isChecked()
        # i2i/인페인팅으로 해상도가 잠겨 있으면 랜덤 해상도는 원본 크기를 덮어쓰면 안 되므로 끈다.
        resolution_choices = (
            self.resolution_panel.aspect_random_choices() if self.image_source.size is None else ()
        )
        randomize_resolution = self.random_resolution_check.isChecked() and len(resolution_choices) >= 2
        request = GenerationRequest(
            action=self.image_source.action(),
            prompt=prompt,
            negative_prompt=negative,
            width=size[0],
            height=size[1],
            seed=0 if randomize else self._seed_value(),
            steps=self.steps_spin.value(),
            cfg_scale=self.cfg_spin.value(),
            cfg_rescale=self.rescale_spin.value(),
            sampler=self.sampler_combo.currentText(),
            scheduler=self.scheduler_combo.currentText(),
            model=spec.key,
            uc_preset_id=uc_key,
            characters=self.character_prompts.captions(),
            use_coords=self.character_prompts.use_coords(),
            image=self.image_source.image_bytes,
            mask=self.image_source.mask_bytes,
            strength=self.image_source.strength,
            noise=self.image_source.noise,
            add_original_image=self.image_source.add_original_image,
        )
        return GenerationJob(
            request=request,
            count=count,
            delay_seconds=self.delay_spin.value(),
            save_dir=self._settings.save_dir,
            filename_template=self._settings.filename_template,
            image_format=self._settings.image_format,
            prompt_word_limit=self._settings.prompt_word_limit,  # Req 3.5
            character_word_limit=self._settings.character_word_limit,  # Req 3.6
            randomize_seed=randomize,
            measure_credit=self.measure_credit_action.isChecked(),
            randomize_resolution=randomize_resolution,
            resolution_choices=resolution_choices,
        )

    def _on_generate_once(self) -> None:
        self._start_job(self.build_job(count=1))

    def _on_generate_auto(self) -> None:
        self._start_auto(self.count_spin.value())

    def _on_quick_generate(self, index: int) -> None:
        """퀵 매수 버튼 — 그 매수를 입력란에 넣고 바로 연속 생성한다 (V4.5와 같은 동작).

        입력란까지 같이 바꾸는 이유: 지금 몇 장을 돌리는 중인지 화면에 남고, 종료할 때
        그 값이 설정에 저장되어 다음 실행에도 이어진다.
        """
        counts = self._settings.batch.quick_counts
        if not 0 <= index < len(counts):
            return  # 설정이 4칸을 채우지 못한 경우 — 버튼은 비활성 상태다
        count = counts[index]
        self.count_spin.setValue(count)
        self._start_auto(count)

    def _start_auto(self, count: int) -> None:
        """연속 생성 시작 — 매수 입력란과 퀵 버튼이 함께 쓰는 경로."""
        # 고정 시드로 연속 생성하면 같은 그림만 반복되고 Anlas만 나간다.
        if count != 1 and not self.seed_random_check.isChecked():
            tr = self._i18n.get_text
            QMessageBox.warning(self, tr("errors.title"), tr("errors.fixed_seed_batch"))
            self.status_label.setText(tr("errors.fixed_seed_batch"))
            return
        self._start_job(self.build_job(count=count))

    def _start_job(self, job: GenerationJob) -> None:
        try:
            self._service.start(job)
        except RuntimeError:
            return  # 이미 실행 중 — 버튼 비활성화가 정상이면 도달하지 않음
        self._set_running(True)
        # 첫 ImageStarted가 오기 전에도 즉시 반응을 보여준다
        self.status_label.setText(self._i18n.get_text("statusbar.generating"))

    def _on_stop_clicked(self) -> None:
        """세팅별 연속 생성 중이면 이번 이미지를 끝으로 순환도 멈춘다 (V4와 동일)."""
        self._settings_batch_stop_requested = True
        self._service.stop()

    # ── 세팅별 연속 생성 (V4의 "세팅별 연속 생성") ──────────────

    _SETTINGS_BATCH_MIN_FILES = 2

    def _on_generate_by_settings(self) -> None:
        """세팅 파일 2개 이상을 골라, 생성마다 다음 파일을 불러오며 순환 생성한다.

        총 매수는 `count_spin`을 그대로 쓴다 (0 = 무제한, 일반 연속 생성과 같은 규칙).
        각 세팅 파일로 만든 이미지는 `save_dir/<세팅 파일명>/`에 저장된다 (V4와 동일).
        """
        tr = self._i18n.get_text
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("errors.settings_load_caption"), self._settings.presets_dir, self._SETTINGS_FILE_FILTER
        )
        if not paths:
            return
        if len(paths) < self._SETTINGS_BATCH_MIN_FILES:
            QMessageBox.information(self, tr("errors.warning"), tr("errors.settings_select_min_two"))
            return

        self._settings_batch_paths = paths
        self._settings_batch_index = -1
        self._settings_batch_total = self.count_spin.value()
        self._settings_batch_completed = 0
        self._settings_batch_stop_requested = False
        if not self._advance_settings_batch():
            self._settings_batch_paths = []

    def _next_settings_batch_index(self) -> int:
        """다음에 쓸 세팅 파일의 자리. 기본은 고른 순서대로, 옵션을 켜면 무작위.

        무작위일 때 같은 파일이 연달아 두 번 나오지는 않게 한다 — 세팅을 여러 개
        고른 이유가 번갈아 쓰려는 것이기 때문이다 (파일이 2개면 결국 번갈아 돈다).
        """
        total = len(self._settings_batch_paths)
        if not self._settings.batch.random_settings_order or total < 2:
            return (self._settings_batch_index + 1) % total
        choices = [i for i in range(total) if i != self._settings_batch_index]
        return random.choice(choices)

    def _advance_settings_batch(self) -> bool:
        """다음 세팅 파일을 불러와 적용하고 그 파일로 이미지 1장을 생성한다.

        진행 중 이미지에는 영향이 없다 — 매 순환마다 새 `GenerationJob`(count=1)을
        만들어 시작한다. 성공하면 True, 파일을 읽지 못하거나 이미 실행 중이면 False.
        """
        tr = self._i18n.get_text
        self._settings_batch_index = self._next_settings_batch_index()
        path = self._settings_batch_paths[self._settings_batch_index]

        defaults = self._get_current_preset_config().model_dump()
        try:
            loaded = settings_file.load(Path(path), defaults=defaults)
        except PresetError as e:
            QMessageBox.warning(self, tr("errors.title"), str(e))
            return False

        self._on_preset_loaded(loaded.preset)
        if loaded.seed is not None:
            self.seed_random_check.setChecked(False)
            self.seed_edit.setText(str(loaded.seed))

        stem = Path(path).stem
        job = self.build_job(count=1)
        job = dataclasses.replace(job, save_dir=str(Path(job.save_dir) / stem))
        try:
            self._service.start(job)
        except RuntimeError:
            return False
        self._set_running(True)
        if self._settings_batch_total:
            self.status_label.setText(
                tr(
                    "statusbar.by_settings_progress",
                    self._settings_batch_completed + 1,
                    self._settings_batch_total,
                    stem,
                )
            )
        else:
            self.status_label.setText(
                tr("statusbar.by_settings_progress_inf", self._settings_batch_completed + 1, stem)
            )
        return True

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        self._refresh_generate_buttons()

    def _refresh_generate_buttons(self) -> None:
        """생성 버튼 활성 조건은 두 가지다 — 실행 중이 아니고, 로그인되어 있을 것."""
        can_start = not self._is_running and self._logged_in
        self.once_button.setEnabled(can_start)
        self.auto_button.setEnabled(can_start)
        self.by_settings_button.setEnabled(can_start)
        self.stop_button.setEnabled(self._is_running)
        self._refresh_quick_buttons()

    # ── 서비스 이벤트 (메인 스레드에서 수신) ──────────────

    def _on_generation_event(self, event: GenerationEvent) -> None:
        tr = self._i18n.get_text
        if isinstance(event, JobStarted):
            self._job_total = event.total
        elif isinstance(event, ImageStarted):
            if self._job_total == 1:
                self.status_label.setText(tr("statusbar.generating"))
            elif self._job_total in (0, None):
                self.status_label.setText(tr("statusbar.auto_generating_inf"))
            else:
                self.status_label.setText(tr("statusbar.auto_generating_count", event.index, self._job_total))
        elif isinstance(event, WaitingNext):
            self.status_label.setText(tr("statusbar.auto_wait", _format_seconds(event.wait_seconds)))
        elif isinstance(event, ImageRetrying):
            self.status_label.setText(tr("statusbar.auto_error_wait", int(event.wait_seconds)))
        elif isinstance(event, ImageCompleted):
            self._show_image(event.path)
            # 저장 위치를 바로 확인할 수 있게 파일명 표시 + 전체 경로 툴팁
            self.status_label.setToolTip(event.path)
            self.preview_label.setToolTip(event.path)
            # M3: Gallery — 새 이미지 자동 추가
            if self._gallery_view is not None:
                self._gallery_view.append_image(event.path)
        elif isinstance(event, JobFinished):
            continuing = False
            in_settings_batch = bool(self._settings_batch_paths)
            if in_settings_batch:
                # 세팅별 연속 생성 도중 — 이 잡은 세팅 파일 하나로 만든 이미지 1장이다.
                stopped = event.stopped or self._settings_batch_stop_requested
                if event.error is None and not stopped:
                    self._settings_batch_completed += 1
                    more = (
                        self._settings_batch_total == 0
                        or self._settings_batch_completed < self._settings_batch_total
                    )
                    continuing = more
                if not continuing:
                    self._settings_batch_paths = []
                    # 총 진행량은 이번 잡의 1장이 아니라 순환 전체의 누적 매수다.
                    event = dataclasses.replace(
                        event, completed=self._settings_batch_completed, stopped=stopped
                    )

            if not continuing:
                self._set_running(False)
                if event.error is not None:
                    key = _ERROR_TYPE_TO_KEY.get(event.error_type or "", "errors.generation_error")
                    self.status_label.setText(tr("statusbar.job_error", tr(key)))
                elif event.stopped:
                    self.status_label.setText(tr("statusbar.job_stopped", event.completed))
                else:
                    self.status_label.setText(tr("statusbar.job_finished", event.completed))
            # M3: Credit Estimator — compute and store batch cost from observations
            if event.credit_observations and event.completed > 0:
                size = self.target_size()
                steps = self.steps_spin.value()
                from ..core.credit_estimator import CreditObservation

                observations = [
                    obs
                    if isinstance(obs, CreditObservation)
                    else CreditObservation(index=obs.index, percent=obs.percent, timestamp=obs.timestamp)
                    for obs in event.credit_observations
                ]
                cost = self._credit_estimator.compute_batch_cost(
                    observations=observations,
                    image_count=event.completed,
                    resolution=size,
                    steps=steps,
                )
                if cost is not None:
                    self._credit_estimator.store_cost(cost)
                # Update gauge with latest percent if observations available
                if observations:
                    latest_percent = observations[-1].percent
                    self._update_credit_gauge(latest_percent)
            self.refresh_anlas()

            if continuing:
                # 일반 연속 생성과 같은 간격 규칙을 세팅 파일이 바뀌는 순간에도 지킨다.
                # count=1짜리 개별 잡은 자기 안에서 대기하지 않으므로 여기서 직접 기다린다.
                delay_ms = int(self.delay_spin.value() * 1000)
                if delay_ms > 0:
                    QTimer.singleShot(delay_ms, self._advance_settings_batch_after_delay)
                else:
                    self._advance_settings_batch_after_delay()

    def _advance_settings_batch_after_delay(self) -> None:
        """세팅별 연속 생성 간격 대기 후 호출 — 그사이 중지됐으면 순환을 끝낸다."""
        if not shiboken6.isValid(self):
            return  # 대기 중 창이 닫힌 경우 — QTimer가 죽은 위젯을 참조하면 세그폴트로 이어진다
        if not self._settings_batch_paths:
            return  # 대기 중 중지되었거나 이미 정리된 경우
        if self._settings_batch_stop_requested or not self._advance_settings_batch():
            self._settings_batch_paths = []
            self._set_running(False)
            tr = self._i18n.get_text
            if self._settings_batch_stop_requested:
                self.status_label.setText(tr("statusbar.job_stopped", self._settings_batch_completed))
            else:
                self.status_label.setText(tr("statusbar.job_finished", self._settings_batch_completed))

    def _show_image(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        self.preview_label.setPixmap(pixmap)  # ZoomableImageView가 뷰포트에 맞춰 앉힌다

    # ── 새 버전 확인 ─────────────────────────────────────

    def check_for_updates(self, *, manual: bool) -> None:
        """백그라운드로 최신 릴리스를 확인한다 (Anlas 조회와 같은 패턴).

        manual=False(시작 시 자동)는 새 버전이 있을 때만 상태바에 조용히 알린다 —
        실행하자마자 모달을 띄우지 않는다.
        """
        if manual:
            self.status_label.setText(self._i18n.get_text("updates.checking"))

        def fetch() -> None:
            release = check_for_update(__version__)
            self._emit_safely(self._update_checked, release, manual)

        self._run_in_background(fetch, "naiauto-updates")

    def _on_update_checked(self, release: object, manual: bool) -> None:
        tr = self._i18n.get_text
        if not isinstance(release, ReleaseInfo):
            if manual:  # 최신이거나 확인 실패 — 자동 확인이면 아무 말도 하지 않는다
                self.status_label.setText(tr("updates.up_to_date", __version__))
            return

        self.status_label.setText(tr("updates.available", release.tag))
        if not manual:
            return  # 시작 시 확인은 상태바까지만
        answer = QMessageBox.question(
            self,
            tr("updates.menu"),
            tr("updates.available_body", release.tag, __version__),
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Close,
            QMessageBox.StandardButton.Open,
        )
        if answer == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl(release.url or RELEASES_PAGE))

    # ── Anlas (백그라운드 조회) ───────────────────────────

    def _run_in_background(self, fn: Callable[[], None], name: str) -> None:
        """조회를 백그라운드에서 돌린다 (UI를 막지 않기 위해).

        모든 백그라운드 조회가 이 한 곳을 지난다. 테스트는 이 메서드를 동기 실행으로
        바꿔 끼워, 창이 사라진 뒤 스레드가 시그널을 쏘는 경합을 아예 없앤다.
        """
        threading.Thread(target=fn, name=name, daemon=True).start()

    def _emit_safely(self, signal: SignalInstance, *args: object) -> None:
        """창이 아직 살아 있을 때만 시그널을 쏜다.

        조회가 끝나기 전에 창이 닫히면 알릴 곳이 없다. 파괴된 위젯에 emit하면 파이썬
        예외가 아니라 **세그폴트**가 나므로(CI에서 실제로 겪었다), 파이썬 쪽에서 먼저 막는다.
        """
        if not shiboken6.isValid(self):
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            pass  # isValid 확인과 emit 사이에 닫힌 경우

    def refresh_anlas(self) -> None:
        def fetch() -> None:
            try:
                result = self._client.get_anlas()
            except Exception as e:
                result = e
            self._emit_safely(self._anlas_fetched, result)

        self._run_in_background(fetch, "naiauto-anlas")

    def _on_anlas_fetched(self, result: object) -> None:
        """보유 Anlas를 그대로 표시한다.

        V4.5까지는 Opus면 기본 해상도·28스텝 이하가 무제한이라 "∞"로 뭉갰지만,
        V5는 Opus도 자동 충전되는 별도 크레딧을 쓰고 소진되면 Anlas가 나간다.
        따라서 실제 잔액이 항상 보여야 한다. (V5 전용 크레딧 표시는 소모량을
        측정한 뒤 별도 인터페이스로 추가 예정 — PLANNING.md M3)
        """
        tr = self._i18n.get_text
        if not isinstance(result, dict):
            self.anlas_label.setText(f"{tr('misc.anlas')} {tr('misc.unknown')}")
            self.anlas_label.setToolTip("")
            return
        total = result.get("total", 0)
        text = f"{tr('misc.anlas')} {total:,}" if isinstance(total, int) else f"{tr('misc.anlas')} {total}"
        if result.get("opus"):
            text = f"{text}  ·  {tr('misc.opus')}"
        self.anlas_label.setText(text)
        self.anlas_label.setToolTip(
            tr("misc.anlas_breakdown", result.get("fixed", 0), result.get("purchased", 0))
        )
        self._show_usage(result.get("usage"))
        # M3: Update credit gauge after Anlas refresh
        usage = result.get("usage")
        if usage is not None and hasattr(usage, "percent"):
            self._update_credit_gauge(usage.percent)

    def _show_usage(self, usage: OpusUsage | None) -> None:
        """V5 생성 크레딧 게이지. 응답에 usage가 없으면 통째로 숨긴다."""
        tr = self._i18n.get_text
        self.usage_label.setVisible(usage is not None)
        self.usage_bar.setVisible(usage is not None)
        if usage is None:
            return
        self.usage_label.setText(tr("misc.usage_percent", usage.percent))
        self.usage_bar.setValue(max(0, min(100, usage.percent)))
        tooltip = [tr("misc.usage_title", usage.percent)]
        if usage.percent_per_day:
            tooltip.append(tr("misc.usage_refill", f"{usage.percent_per_day:.1f}"))
            tooltip.append(tr("misc.usage_next", _format_duration(usage.seconds_to_next_percent)))
        if usage.is_negative:
            tooltip.append(tr("misc.usage_negative"))
        self.usage_label.setToolTip("\n".join(tooltip))
        self.usage_bar.setToolTip("\n".join(tooltip))

    # ── i18n ─────────────────────────────────────────────

    def _set_language(self, code: str) -> None:
        self._i18n.set_language(code)
        self._settings.language = code

    def _on_language_changed(self, _code: str) -> None:
        self.retranslate()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setWindowTitle(f"NAI-Auto-V5 v{__version__}")
        self.prompt_group.setTitle(tr("ui.prompt_group"))
        self.prompt_tabs.retranslate()
        self.model_label.setText(tr("ui.model"))
        self.resolution_panel.retranslate()
        self.ai_section.retranslate()
        self.sampler_label.setText(tr("image_options.sampler"))
        self.scheduler_label.setText(tr("advanced.noise_schedule"))
        self.steps_label.setText(tr("image_options.steps"))
        self.cfg_label.setText(tr("advanced.prompt_guidance"))
        self.rescale_label.setText(tr("advanced.prompt_guidance_rescale"))
        self.seed_label.setText(tr("image_options.seed"))
        self.seed_random_check.setText(tr("image_options.random"))
        self.uc_preset_label.setText(tr("ui.uc_preset"))
        self.quality_check.setText(tr("ui.quality_tags"))
        self.generate_group.setTitle(tr("generate.title"))
        self.count_label.setText(tr("batch.count"))
        self.delay_label.setText(tr("batch.delay"))
        self.random_resolution_check.setText(tr("batch.random_resolution"))
        self.random_resolution_check.setToolTip(tr("batch.random_resolution_tooltip"))
        self._refresh_quick_buttons()
        self.once_button.setText(tr("generate.once"))
        self.auto_button.setText(tr("generate.auto"))
        self.by_settings_button.setText(tr("generate.by_settings"))
        self.stop_button.setText(tr("generate.stop"))
        self.character_prompts.retranslate()
        self.image_source.retranslate()
        self.file_menu.setTitle(tr("menu.file"))
        self.login_action.setText(tr("menu.login"))
        self.accounts_action.setText(tr("menu.accounts"))
        self.login_label.setText(
            tr("statusbar.logged_in") if self._logged_in else tr("statusbar.before_login")
        )
        self.image_info_action.setText(tr("image_info.menu"))
        self.save_settings_action.setText(tr("menu.save_settings"))
        self.load_settings_action.setText(tr("menu.load_settings"))
        self.options_action.setText(tr("ui.options_menu"))
        self.view_menu.setTitle(tr("menu.view"))
        self.result_panel_action.setText(tr("menu.toggle_panel"))
        self.reset_layout_action.setText(tr("menu.reset_layout"))
        self.image_source_action.setText(tr("image_source.menu"))
        self.tools_menu.setTitle(tr("menu.tools"))
        self.log_action.setText(tr("logs.menu"))
        self.measure_credit_action.setText(tr("logs.measure_credit"))
        self.measure_credit_action.setToolTip(tr("logs.measure_credit_hint"))
        # M3: WD14 / Presets / Gallery actions
        self.wd14_action.setText(tr("menu.wd14_auto_tag"))
        self.presets_action.setText(tr("menu.presets"))
        self.gallery_action.setText(tr("menu.gallery_view"))
        self.folders_menu.setTitle(tr("folders.title"))
        self.open_results_action.setText(tr("folders.results"))
        self.open_wildcards_action.setText(tr("folders.wildcards"))
        self.open_presets_action.setText(tr("folders.presets"))
        self.etc_menu.setTitle(tr("menu.etc"))
        self.update_action.setText(tr("updates.menu"))
        self.language_menu.setTitle(tr("menu.languages"))
        if self.preview_label.pixmap().isNull():
            self.preview_label.setText(f"{tr('result.no_image')}\n\n{tr('image_info.drop_hint')}")
        if not self.status_label.text():
            self.status_label.setText(tr("statusbar.idle"))

    _job_total: int | None = None
    _is_running: bool = False

    # ── 세팅별 연속 생성 (V4 parity) — `_settings_batch_paths`는 리스트라
    # 클래스 속성 기본값으로 두면 인스턴스끼리 공유되므로 `__init__`에서 초기화한다.
    _settings_batch_index: int = -1
    _settings_batch_total: int = 0
    _settings_batch_completed: int = 0
    _settings_batch_stop_requested: bool = False
