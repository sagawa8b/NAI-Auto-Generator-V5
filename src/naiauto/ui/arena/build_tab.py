"""조합 생성 탭 — 작가를 섞어 조합을 만들고, 그 조합의 그림을 뽑는다.

두 단계를 한 화면에서 한다.

1. **조합 만들기** — 명단에서 작가를 뽑아 가중치를 붙인다. 그림은 아직 없다.
2. **그림 뽑기** — 그림이 없는 조합의 그림을 순서대로 생성한다. 실제로 크레딧을
   쓰는 유일한 버튼이라, 몇 장을 뽑을지 누르기 전에 보여 준다.

**모든 조합이 같은 시드·같은 기본 프롬프트로 뽑힌다.** 그래야 남는 차이가 그림체뿐이다.
기본 프롬프트는 메인 창의 것을 그대로 쓰고 여기서는 읽기 전용으로 보여 준다 — 아레나가
자기 프롬프트를 따로 들면 메인 창과 어긋나서 "왜 다른 그림이 나오지" 하게 된다.

## 화면을 좌우로 나눈다

예전에는 설정 여덟 줄과 프롬프트 미리보기가 위쪽을 다 먹고, 정작 만든 조합은 맨 아래에서
네댓 줄만 보였다. 창 폭이 1000px인데 좌우로 나뉜 곳이 하나도 없었다.

- **왼쪽은 조작값** — 규칙·뽑기·기본 프롬프트를 접이식으로 두고, 접힌 동안에는 현재 값을
  요약 한 줄로 보여 준다 (옵션 창이 쓰는 `CollapsibleSection` 그대로). 처음에는 규칙만
  펼쳐 둔다. **와일드카드 경고만은 접이식 밖에** 있다 — 접어 두면 보이지 않는 경고가 된다.
- **오른쪽은 결과** — 조합 큐가 화면의 대부분을 쓰고, 줄마다 **썸네일**이 붙는다. 뽑아 둔
  그림을 이 탭에서 볼 방법이 없어서 어느 줄이 무엇인지 태그 문자열로만 가늠해야 했다.
- **`그림 생성`은 주 동작** — 앱에서 크레딧을 쓰는 유일한 버튼인데 공짜인
  `랜덤 조합 만들기`와 똑같이 생겨 있었다. 칠하고, 몇 장을 뽑을지 라벨에 적는다.
"""

from __future__ import annotations

import dataclasses
import logging

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.api.model_specs import get_spec
from ...core.arena.combos import (
    format_artist_block,
    format_weight,
    generate_combos,
    name_signature,
    normalize_weight,
    parse_artist_block,
    round_weight,
)
from ...core.arena.models import (
    CURVE_PRESETS,
    INSERT_POSITIONS,
    MAX_WEIGHT_STEP,
    MIN_WEIGHT_STEP,
    WEIGHT_MODE_CURVE,
    WEIGHT_MODES,
    Combo,
    ComboSlot,
)
from ...services.arena_service import (
    ArenaBatchFinished,
    ArenaBatchStarted,
    ArenaBusyError,
    ArenaImageReady,
    ArenaImageRetrying,
    ArenaImageStarted,
    ArenaSpec,
    ArenaWaitingNext,
    dynamic_prompt_parts,
    pending_combos,
    ready_combos,
)
from ..widgets.collapsible_section import CollapsibleSection
from .base import ArenaTab
from .style import mark_primary
from .thumbnails import combo_icon

logger = logging.getLogger(__name__)

#: 해상도 콤보의 "메인 창 설정" 항목이 들고 있는 값.
RESOLUTION_FOLLOW = (0, 0)

#: 조합 큐 표의 열. 썸네일이 맨 앞이다 — 뽑아 놓은 그림을 이 탭에서 볼 방법이
#: 없어서, 어느 줄이 무엇인지 태그 문자열로만 가늠해야 했다.
COL_THUMBNAIL = 0
COL_STATE = 1
COL_GENERATION = 2
COL_COMBO = 3
QUEUE_COLUMNS = 4

#: 큐 썸네일 한 변 (논리 픽셀).
THUMBNAIL_SIZE = 44

#: 좌우 패널 사이의 여백과 처음 뜰 때의 나눔 비율 (논리 픽셀).
PANEL_GAP = 8
SPLIT_SIZES = (340, 660)

#: 상태줄에 조합을 요약해 넣을 때의 최대 길이.
_STATUS_COMBO_CHARS = 52

#: 접힌 섹션의 요약 한 줄에 넣을 최대 길이.
_SUMMARY_CHARS = 64


def index_of_data(combo: QComboBox, value) -> int:
    """`itemData`가 `value`인 항목의 인덱스 (없으면 -1).

    `QComboBox.findData`를 쓰지 않는 이유: 파이썬 튜플은 QVariant로 넘어가면서
    비교가 어긋나 해상도 항목을 못 찾는다 (문자열 데이터는 잘 찾아서 눈에 안 띈다).
    """
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            return index
    return -1


class _EditComboDialog(QDialog):
    """조합의 작가 블록을 고치는 작은 창.

    자리(이름+가중치)를 하나씩 만지게 하는 대신, 완성된 프롬프트 문자열을 그대로
    보여 주고 손보게 한다 — `1.2::artist:a, artist:b::` 같은 가중치 문법에 익숙한
    사용자에게는 이쪽이 빠르고, 파싱은 `parse_artist_block`이 관대하게 받아 준다.
    """

    def __init__(self, i18n, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        tr = i18n.get_text
        self.setWindowTitle(tr("arena.queue_edit_title"))
        layout = QVBoxLayout(self)

        self.label = QLabel(tr("arena.queue_edit_prompt"))
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        self.editor.setMinimumHeight(120)
        layout.addWidget(self.editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(480, 240)

    @classmethod
    def edit(cls, i18n, text: str, parent: QWidget | None = None) -> str | None:
        """편집 창을 띄운다. 확인이면 새 문자열, 취소면 None."""
        dialog = cls(i18n, text, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.editor.toPlainText()


class BuildTab(ArenaTab):
    """조합 생성 + 이미지 프리페치."""

    KEY = "build"

    def __init__(self, i18n, settings, service, request_provider, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)
        self._request_provider = request_provider

        root = QVBoxLayout(self)
        # 폭이 1000px인데 세로로만 쌓아 올려 정작 결과물인 큐가 네댓 줄만 보였다.
        # 왼쪽은 조작값, 오른쪽은 결과. 좁은 화면에서는 한쪽을 접을 수 있다.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, 1)

        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, PANEL_GAP, 0)

        # ── 조합 규칙 ────────────────────────────────────────────────────
        rules_body = QWidget()
        rules = QFormLayout(rules_body)
        rules.setContentsMargins(0, 0, 0, 0)

        count_row = QHBoxLayout()
        self.min_spin = QSpinBox()
        self.min_spin.setRange(1, 10)
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 10)
        self.min_spin.valueChanged.connect(self._sync_count_bounds)
        self.max_spin.valueChanged.connect(self._sync_count_bounds)
        count_row.addWidget(self.min_spin)
        self.count_tilde = QLabel("~")
        count_row.addWidget(self.count_tilde)
        count_row.addWidget(self.max_spin)
        count_row.addStretch(1)
        self.count_label = QLabel()
        rules.addRow(self.count_label, count_row)

        weight_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        for mode in WEIGHT_MODES:
            self.mode_combo.addItem("", mode)
        self.mode_combo.currentIndexChanged.connect(self._sync_curve_enabled)
        weight_row.addWidget(self.mode_combo)
        self.curve_combo = QComboBox()
        for curve in CURVE_PRESETS:
            self.curve_combo.addItem("", curve)
        weight_row.addWidget(self.curve_combo)
        self.wmin_spin = QDoubleSpinBox()
        self.wmax_spin = QDoubleSpinBox()
        for spin in (self.wmin_spin, self.wmax_spin):
            spin.setRange(-2.0, 3.0)
            spin.setDecimals(2)
        # 어느 쪽이 최소이고 어느 쪽이 최대인지 보이게 라벨을 붙인다 — 값 두 개가
        # 나란히 있기만 하면 어느 쪽을 만져야 할지 알 수 없다.
        self.wmin_label = QLabel()
        self.wmax_label = QLabel()
        weight_row.addWidget(self.wmin_label)
        weight_row.addWidget(self.wmin_spin)
        self.weight_tilde = QLabel("~")
        weight_row.addWidget(self.weight_tilde)
        weight_row.addWidget(self.wmax_label)
        weight_row.addWidget(self.wmax_spin)
        self.wmin_spin.valueChanged.connect(self._sync_weight_bounds)
        self.wmax_spin.valueChanged.connect(self._sync_weight_bounds)
        weight_row.addStretch(1)
        self.weight_label = QLabel()
        rules.addRow(self.weight_label, weight_row)

        # 증감 폭 — 가중치를 얼마나 잘게 나눌지
        step_row = QHBoxLayout()
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(MIN_WEIGHT_STEP, MAX_WEIGHT_STEP)
        self.step_spin.setDecimals(2)
        self.step_spin.setSingleStep(0.01)
        self.step_spin.valueChanged.connect(self._on_step_changed)
        step_row.addWidget(self.step_spin)
        self.step_hint = QLabel()
        step_row.addWidget(self.step_hint, 1)
        self.step_label = QLabel()
        rules.addRow(self.step_label, step_row)

        place_row = QHBoxLayout()
        self.insert_combo = QComboBox()
        for position in INSERT_POSITIONS:
            self.insert_combo.addItem("", position)
        place_row.addWidget(self.insert_combo)
        self.prefix_check = QCheckBox()
        place_row.addWidget(self.prefix_check)
        place_row.addStretch(1)
        self.insert_label = QLabel()
        rules.addRow(self.insert_label, place_row)

        self.rules_group = CollapsibleSection(i18n, "arena.build_rules")
        self.rules_group.set_content(rules_body)
        # 처음에는 규칙만 펼쳐 둔다 — 여기가 실제로 손대는 값이고, 나머지는 요약
        # 한 줄이면 충분하다.
        self.rules_group.set_expanded(True)
        layout.addWidget(self.rules_group)

        # ── 뽑기 ────────────────────────────────────────────────────────
        batch_body = QWidget()
        batch = QFormLayout(batch_body)
        batch.setContentsMargins(0, 0, 0, 0)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 200)
        self.batch_size_label = QLabel()
        batch.addRow(self.batch_size_label, self.batch_spin)

        self.resolution_combo = QComboBox()
        self.resolution_label = QLabel()
        batch.addRow(self.resolution_label, self.resolution_combo)

        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 50)
        self.threshold_label = QLabel()
        batch.addRow(self.threshold_label, self.threshold_spin)

        self.batch_group = CollapsibleSection(i18n, "arena.build_batch")
        self.batch_group.set_content(batch_body)
        layout.addWidget(self.batch_group)

        # ── 수동 추가 ────────────────────────────────────────────────────
        # 무작위 생성 말고, 원하는 작가·가중치로 조합 하나를 직접 만든다. 명단에
        # 없는 작가도 그대로 쓸 수 있다 (편집 창과 같은 `1.2::artist:이름 ::` 문법).
        manual_body = QWidget()
        manual_layout = QVBoxLayout(manual_body)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        self.manual_hint = QLabel()
        self.manual_hint.setWordWrap(True)
        self.manual_hint.setStyleSheet("color: palette(mid);")
        manual_layout.addWidget(self.manual_hint)
        self.manual_edit = QPlainTextEdit()
        self.manual_edit.setMinimumHeight(60)
        manual_layout.addWidget(self.manual_edit)
        manual_row = QHBoxLayout()
        self.manual_weight_label = QLabel()
        manual_row.addWidget(self.manual_weight_label)
        self.manual_weight_spin = QDoubleSpinBox()
        self.manual_weight_spin.setRange(-2.0, 3.0)
        self.manual_weight_spin.setSingleStep(0.05)
        self.manual_weight_spin.setDecimals(2)
        self.manual_weight_spin.setValue(1.0)
        manual_row.addWidget(self.manual_weight_spin)
        manual_row.addStretch(1)
        self.manual_add_button = QPushButton()
        self.manual_add_button.clicked.connect(self.add_manual_combo)
        manual_row.addWidget(self.manual_add_button)
        manual_layout.addLayout(manual_row)
        self.manual_group = CollapsibleSection(i18n, "arena.manual_group")
        self.manual_group.set_content(manual_body)
        layout.addWidget(self.manual_group)

        # ── 기본 프롬프트 (읽기 전용) ────────────────────────────────────
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMinimumHeight(132)
        self.prompt_group = CollapsibleSection(i18n, "arena.base_prompt")
        self.prompt_group.set_content(self.prompt_view)
        layout.addWidget(self.prompt_group)

        # 경고는 접이식 **밖에** 둔다 — 접어 두면 보이지 않는 경고가 된다.
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)
        layout.addStretch(1)
        self.splitter.addWidget(left)

        # ── 오른쪽: 만들어 둔 조합 ──────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(PANEL_GAP, 0, 0, 0)

        self.summary_label = QLabel()
        right_layout.addWidget(self.summary_label)

        self.queue_table = QTableWidget(0, QUEUE_COLUMNS)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.verticalHeader().setDefaultSectionSize(THUMBNAIL_SIZE + 8)
        self.queue_table.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 우클릭 메뉴로 고른 조합을 고치거나·복사하거나·지운다 (제보: 표에서 바로
        # 손대고 싶다). 표 스스로 그리게 두면 빈 곳을 눌러도 뜨므로 커스텀으로 건다.
        self.queue_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_table.customContextMenuRequested.connect(self._show_queue_menu)
        # F2 수정·Ctrl+C 복사 — 표 위젯이 이 키를 먼저 먹어 keyPressEvent까지 오지
        # 않으므로, 표에 포커스가 있을 때만 사는 QShortcut으로 가로챈다 (월드컵 탭의
        # 판정 단축키와 같은 방식).
        self._queue_shortcuts = self._build_queue_shortcuts()
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(COL_COMBO, QHeaderView.ResizeMode.Stretch)
        for col in (COL_THUMBNAIL, COL_STATE, COL_GENERATION):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        right_layout.addWidget(self.queue_table, 1)

        queue_actions = QHBoxLayout()
        self.remove_button = QPushButton()
        self.remove_button.clicked.connect(self.remove_selected)
        queue_actions.addWidget(self.remove_button)
        self.remove_pending_button = QPushButton()
        self.remove_pending_button.clicked.connect(self.remove_pending)
        queue_actions.addWidget(self.remove_pending_button)
        self.undo_button = QPushButton()
        self.undo_button.clicked.connect(self.undo_removal)
        queue_actions.addWidget(self.undo_button)
        queue_actions.addStretch(1)
        self.delete_images_check = QCheckBox()
        self.delete_images_check.toggled.connect(self._on_delete_images_toggled)
        queue_actions.addWidget(self.delete_images_check)
        right_layout.addLayout(queue_actions)

        # ── 동작 ────────────────────────────────────────────────────────
        actions = QHBoxLayout()
        self.make_button = QPushButton()
        self.make_button.clicked.connect(self.make_combos)
        actions.addWidget(self.make_button, 1)
        # 앱에서 **크레딧을 쓰는 유일한 버튼**이다. 주 동작으로 칠하고, 몇 장을
        # 뽑을지 라벨에 적는다 — 누르기 전에 알 수 있어야 한다.
        self.generate_button = QPushButton()
        mark_primary(self.generate_button)
        self.generate_button.clicked.connect(self.start_generation)
        actions.addWidget(self.generate_button, 1)
        self.stop_button = QPushButton()
        self.stop_button.clicked.connect(self._service.stop)
        self.stop_button.setEnabled(False)
        actions.addWidget(self.stop_button)
        right_layout.addLayout(actions)
        self.splitter.addWidget(right)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes(list(SPLIT_SIZES))

        # 본문 위젯이 다 생긴 뒤에야 요약을 만들 수 있다.
        self.rules_group.set_summary_provider(self._rules_summary)
        self.batch_group.set_summary_provider(self._batch_summary)
        self.manual_group.set_summary_provider(lambda: self.tr("arena.manual_summary"))
        self.prompt_group.set_summary_provider(self._prompt_summary)

        self._load_settings()
        self.retranslate()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._refresh_prompt()
        self._refresh_summary()
        self._refresh_queue()
        running = self._service.is_running
        # **조합 만들기는 생성 중에도 열어 둔다.** 만드는 것은 공짜이고, 돌고 있는
        # 잡은 시작할 때 뜬 목록(`ArenaService._batch`)만 보므로 새 조합이 끼어들지
        # 않는다. 막아 두면 처음에 조합을 여러 번 나눠 만들 수가 없다.
        self.make_button.setEnabled(True)
        self.generate_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # 반면 **지우는 것**은 막는다 — 뽑고 있는 조합을 지우면 방금 쓴 크레딧이
        # 갈 곳을 잃는다.
        self.remove_button.setEnabled(not running)
        self.remove_pending_button.setEnabled(not running and bool(pending_combos(self.state)))
        self.undo_button.setEnabled(not running and self._service.can_undo)

    def commit(self) -> None:
        """화면 값 → `settings.arena`. 다음에 켜도 그대로 남는다."""
        arena = self.arena
        arena.min_artists = self.min_spin.value()
        arena.max_artists = self.max_spin.value()
        arena.weight_mode = self.mode_combo.currentData()
        arena.curve = self.curve_combo.currentData()
        arena.weight_min = self.wmin_spin.value()
        arena.weight_max = self.wmax_spin.value()
        arena.weight_step = self.step_spin.value()
        arena.insert_position = self.insert_combo.currentData()
        arena.use_prefix = self.prefix_check.isChecked()
        arena.batch_size = self.batch_spin.value()
        arena.prefetch_threshold = self.threshold_spin.value()
        arena.delete_images_with_combo = self.delete_images_check.isChecked()
        width, height = self.resolution_combo.currentData() or RESOLUTION_FOLLOW
        arena.width, arena.height = width, height

    def retranslate(self) -> None:
        tr = self.tr
        for section in (self.rules_group, self.batch_group, self.manual_group, self.prompt_group):
            section.retranslate()
        self.count_label.setText(tr("arena.artist_count"))
        self.manual_hint.setText(tr("arena.manual_hint"))
        self.manual_weight_label.setText(tr("arena.manual_weight"))
        self.manual_edit.setPlaceholderText(tr("arena.manual_placeholder"))
        self.manual_add_button.setText(tr("arena.manual_add"))
        self.weight_label.setText(tr("arena.weight"))
        self.wmin_label.setText(tr("arena.weight_min"))
        self.wmax_label.setText(tr("arena.weight_max"))
        self.step_label.setText(tr("arena.weight_step"))
        self.step_spin.setToolTip(tr("arena.weight_step_hint"))
        self._refresh_step_hint()
        self.insert_label.setText(tr("arena.insert_position"))
        self.batch_size_label.setText(tr("arena.batch_size"))
        self.resolution_label.setText(tr("arena.resolution"))
        self.threshold_label.setText(tr("arena.prefetch_threshold"))
        self.warning_label.setText(tr("arena.dynamic_warning"))
        self.make_button.setText(tr("arena.make_combos"))
        self._refresh_generate_label()
        self.stop_button.setText(tr("arena.stop"))
        self.prefix_check.setText(tr("arena.use_prefix"))
        self.remove_button.setText(tr("arena.queue_remove"))
        self.remove_pending_button.setText(tr("arena.queue_remove_pending"))
        self.undo_button.setText(tr("arena.undo"))
        self.undo_button.setToolTip(tr("arena.undo_hint"))
        self.delete_images_check.setText(tr("arena.delete_images_with_combo"))
        self.delete_images_check.setToolTip(tr("arena.delete_images_with_combo_hint"))
        self.queue_table.setHorizontalHeaderLabels(
            [
                "",  # 썸네일 — 머리글을 붙이면 그림보다 글이 넓어진다
                tr("arena.queue_col_state"),
                tr("arena.col_generation"),
                tr("arena.queue_col_combo"),
            ]
        )

        mode_keys = {
            "random": "arena.mode_random",
            "balanced": "arena.mode_balanced",
            "curve": "arena.mode_curve",
        }
        for index in range(self.mode_combo.count()):
            self.mode_combo.setItemText(index, tr(mode_keys[self.mode_combo.itemData(index)]))
        for index in range(self.curve_combo.count()):
            self.curve_combo.setItemText(index, tr(f"arena.curve_{self.curve_combo.itemData(index)}"))
        for index in range(self.insert_combo.count()):
            self.insert_combo.setItemText(index, tr(f"arena.insert_{self.insert_combo.itemData(index)}"))
        self._fill_resolutions()
        self.refresh()

    def on_arena_event(self, event) -> None:
        """생성이 어디까지 갔는지 상태줄에 그대로 옮긴다 (진행 막대는 창이 그린다)."""
        tr = self.tr
        if isinstance(event, ArenaBatchStarted):
            self.status_message.emit(tr("arena.generation_started").format(event.total, event.seed))
        elif isinstance(event, ArenaImageStarted):
            self.status_message.emit(
                tr("arena.generating").format(event.index, event.total, self._combo_summary(event.combo_id))
            )
        elif isinstance(event, ArenaImageReady):
            self.status_message.emit(
                tr("arena.generated").format(event.index, event.total, event.total - event.index)
            )
        elif isinstance(event, ArenaImageRetrying):
            self.status_message.emit(
                tr("arena.generation_retry").format(
                    event.index, event.total, event.reason, round(event.wait_seconds), event.attempt
                )
            )
        elif isinstance(event, ArenaWaitingNext):
            self.status_message.emit(
                tr("arena.generation_waiting").format(
                    event.next_index, event.total, round(event.wait_seconds)
                )
            )
        elif isinstance(event, ArenaBatchFinished):
            self._report_finish(event)
        self.refresh()

    # ── 값 싣기 ─────────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        arena = self.arena
        self.min_spin.setValue(arena.min_artists)
        self.max_spin.setValue(arena.max_artists)
        self._select(self.mode_combo, arena.weight_mode)
        self._select(self.curve_combo, arena.curve)
        self.step_spin.setValue(min(MAX_WEIGHT_STEP, max(MIN_WEIGHT_STEP, arena.weight_step)))
        self.wmin_spin.setValue(arena.weight_min)
        self.wmax_spin.setValue(arena.weight_max)
        self._on_step_changed()
        self._select(self.insert_combo, arena.insert_position)
        self.prefix_check.setChecked(arena.use_prefix)
        self.batch_spin.setValue(arena.batch_size)
        self.threshold_spin.setValue(arena.prefetch_threshold)
        self.delete_images_check.setChecked(arena.delete_images_with_combo)
        self._sync_curve_enabled()

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        index = index_of_data(combo, value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _fill_resolutions(self) -> None:
        """지금 고른 모델이 실제로 지원하는 크기만 올린다.

        아레나는 그림체만 보는 습작이라 작게 뽑아 크레딧을 아끼는 쪽이 낫다. 그래서
        메인 창과 별도로 고를 수 있게 했지만, 크기 목록은 모델 스펙에서만 가져온다 —
        지어낸 크기를 보냈다가 서버에 거부당하면 그것대로 크레딧과 시간을 버린다.
        """
        current = (self.arena.width, self.arena.height)
        self.resolution_combo.clear()
        self.resolution_combo.addItem(self.tr("arena.resolution_follow"), RESOLUTION_FOLLOW)
        try:
            spec = get_spec(self._request_provider().model)
        except Exception as e:  # 모델을 못 찾아도 "메인 창 설정"은 늘 쓸 수 있다
            logger.debug("cannot list resolutions for arena: %s", e)
            return
        for width, height in spec.resolutions:
            self.resolution_combo.addItem(f"{width}×{height}", (width, height))
        self._select(self.resolution_combo, current)

    def _sync_count_bounds(self) -> None:
        """최소가 최대를 넘지 않게 서로를 민다."""
        if self.min_spin.value() > self.max_spin.value():
            sender = self.sender()
            if sender is self.min_spin:
                self.max_spin.setValue(self.min_spin.value())
            else:
                self.min_spin.setValue(self.max_spin.value())

    def _sync_weight_bounds(self) -> None:
        """최소가 최대를 넘지 않게 서로를 민다 (작가 수 칸과 같은 규칙).

        예전에는 뒤집힌 값을 조합을 만들 때 조용히 맞바꿨는데, 화면에는 뒤집힌 채로
        남아 있어 무엇이 적용됐는지 알 수 없었다.
        """
        if self.wmin_spin.value() <= self.wmax_spin.value():
            return
        if self.sender() is self.wmin_spin:
            self.wmax_spin.setValue(self.wmin_spin.value())
        else:
            self.wmin_spin.setValue(self.wmax_spin.value())

    def _on_step_changed(self) -> None:
        """증감 폭이 바뀌면 최소·최대 칸의 화살표도 그 폭으로 움직인다."""
        step = self.step_spin.value()
        self.wmin_spin.setSingleStep(step)
        self.wmax_spin.setSingleStep(step)
        self._refresh_step_hint()

    def _refresh_step_hint(self) -> None:
        """이 폭이면 어떤 값이 나오는지 예를 보여 준다 — 숫자만으로는 잘 와닿지 않는다."""
        step = self.step_spin.value()
        base = round_weight(self.arena.weight_min + (self.arena.weight_max - self.arena.weight_min) / 2, step)
        sample = ", ".join(format_weight(base + step * i) for i in range(3))
        self.step_hint.setText(self.tr("arena.weight_step_sample").format(sample))

    def _sync_curve_enabled(self) -> None:
        self.curve_combo.setEnabled(self.mode_combo.currentData() == WEIGHT_MODE_CURVE)

    # ── 화면 갱신 ───────────────────────────────────────────────────────

    def _refresh_prompt(self) -> None:
        """실제로 보낼 조건을 그대로 보여 준다.

        예전에는 작가 블록과 메인 프롬프트만 보여 줬다. 그런데 **네거티브와 캐릭터
        프롬프트도 함께 나간다** — 화면에 없으니 캐릭터 프롬프트에 든 와일드카드
        때문에 경고가 떠도 어디를 봐야 할지 알 수 없었다.
        """
        try:
            request = self._request_provider()
        except Exception as e:  # 메인 창이 아직 준비되지 않았을 수 있다
            logger.debug("no base request for arena: %s", e)
            return
        tr = self.tr
        lines = [tr("arena.preview_artists").format(self._sample_block() or "…")]
        lines.append(tr("arena.preview_prompt").format(request.prompt.strip() or "…"))
        if request.negative_prompt.strip():
            lines.append(tr("arena.preview_negative").format(request.negative_prompt.strip()))
        for index, caption in enumerate(request.characters, start=1):
            text = caption.prompt.strip() or "…"
            if caption.uc.strip():
                text = tr("arena.preview_character_uc").format(text, caption.uc.strip())
            lines.append(tr("arena.preview_character").format(index, text))
        self.prompt_view.setPlainText("\n".join(lines))

        parts = dynamic_prompt_parts(self._spec(request))
        self.warning_label.setVisible(bool(parts))
        if parts:
            named = ", ".join(tr(f"arena.part_{part}") for part in parts)
            self.warning_label.setText(tr("arena.dynamic_warning").format(named))

    def _sample_block(self) -> str:
        """미리보기용 작가 블록 — **가장 최근에 만든** 조합을 보여 준다.

        예전에는 `combos[0]`, 즉 가장 오래된 조합을 보여 줬다. 그래서 가중치 범위를
        좁히고 새 조합을 만들어도 미리보기에는 예전 범위로 만든 값이 그대로 떠 있어,
        설정이 안 먹은 것처럼 보였다.
        """
        if not self.state.combos:
            return ""
        return format_artist_block(self.state.combos[-1].slots, self.arena.use_prefix)

    def _refresh_summary(self) -> None:
        total = len(self.state.combos)
        ready = len(ready_combos(self.state))
        self.summary_label.setText(self.tr("arena.build_summary").format(total, ready, total - ready))
        self._refresh_generate_label()
        for section in (self.rules_group, self.batch_group, self.manual_group, self.prompt_group):
            section.refresh_summary()

    def _refresh_generate_label(self) -> None:
        """`그림 생성` 라벨에 몇 장을 뽑을지 적는다.

        앱에서 크레딧을 쓰는 버튼은 이것 하나뿐인데, 공짜인 `랜덤 조합 만들기`와
        똑같이 생겨 있었다. 장수를 라벨에 넣으면 버튼 자신이 값을 말한다.
        """
        tr = self.tr
        pending = len(pending_combos(self.state))
        if pending:
            self.generate_button.setText(tr("arena.start_generation_n").format(pending))
        else:
            self.generate_button.setText(tr("arena.start_generation"))

    # ── 접힌 섹션의 요약 한 줄 ──────────────────────────────────────────

    def _rules_summary(self) -> str:
        return self.tr("arena.rules_summary").format(
            self.min_spin.value(),
            self.max_spin.value(),
            self.mode_combo.currentText(),
            format_weight(self.wmin_spin.value()),
            format_weight(self.wmax_spin.value()),
            format_weight(self.step_spin.value()),
        )

    def _batch_summary(self) -> str:
        return self.tr("arena.batch_summary").format(
            self.batch_spin.value(),
            self.resolution_combo.currentText(),
            self.threshold_spin.value(),
        )

    def _prompt_summary(self) -> str:
        """접혀 있을 때도 무슨 프롬프트로 뽑는지는 보이게 — 한 줄로 줄인다."""
        text = " · ".join(
            line.strip() for line in self.prompt_view.toPlainText().splitlines() if line.strip()
        )
        return text if len(text) <= _SUMMARY_CHARS else text[: _SUMMARY_CHARS - 1] + "…"

    def _refresh_queue(self) -> None:
        """만들어 둔 조합을 최신순으로 보여 준다 — 방금 만든 것이 맨 위."""
        tr = self.tr
        combos = list(reversed(self.state.combos))
        self.queue_table.setRowCount(len(combos))
        for row, combo in enumerate(combos):
            # 그림뿐 아니라 **전적**도 보여 준다 — 어느 줄이 평가를 담고 있는지
            # 알아야 지워도 되는지 판단할 수 있다.
            if not combo.has_image:
                state = tr("arena.queue_pending")
            elif combo.matches:
                state = tr("arena.queue_rated").format(combo.matches)
            else:
                state = tr("arena.queue_ready")
            cells = (
                (COL_STATE, state),
                (COL_GENERATION, f"Gen.{combo.generation}"),
                (COL_COMBO, format_artist_block(combo.slots, self.arena.use_prefix)),
            )
            for col, text in cells:
                item = QTableWidgetItem(text)
                if col == COL_STATE:
                    # 표를 다시 그려도 어느 줄이 어느 조합인지 잃지 않게.
                    item.setData(Qt.ItemDataRole.UserRole, combo.id)
                self.queue_table.setItem(row, col, item)

            thumbnail = QTableWidgetItem()
            icon = self._thumbnail(combo)
            if icon is not None:
                thumbnail.setIcon(icon)
            self.queue_table.setItem(row, COL_THUMBNAIL, thumbnail)

    def _thumbnail(self, combo) -> QIcon | None:
        """큐 줄에 붙일 작은 그림. 그림이 없거나 못 읽으면 None (빈 칸이 곧 `대기`다)."""
        return combo_icon(self._service.store, combo)

    def selected_combos(self) -> list:
        """고른 줄들의 조합 (중복 없이)."""
        picked = []
        seen = set()
        for row in sorted({item.row() for item in self.queue_table.selectedItems()}):
            item = self.queue_table.item(row, COL_STATE)
            if item is None:
                continue
            combo = self.state.combo(str(item.data(Qt.ItemDataRole.UserRole)))
            if combo is not None and combo.id not in seen:
                picked.append(combo)
                seen.add(combo.id)
        return picked

    # ── 우클릭 메뉴 ─────────────────────────────────────────────────────

    def _show_queue_menu(self, pos: QPoint) -> None:
        """큐 표에서 우클릭 — 고른 조합을 수정·복사·삭제한다.

        빈 곳을 누르면 아무것도 하지 않는다 (골라 둔 줄이 없으면 메뉴를 내지 않는다).
        `수정`은 조합이 하나일 때만 켠다 — 프롬프트 한 덩어리를 고치는 것이라 여러
        개를 한꺼번에 편집할 방법이 없다. `삭제`는 생성 중에는 막는다 (뽑고 있는
        조합을 지우면 방금 쓴 크레딧이 갈 곳을 잃는다 — 버튼과 같은 규칙).
        """
        item = self.queue_table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        # 우클릭한 줄이 선택에 없으면 그 줄만 고른다 — 흔한 파일 탐색기 동작이다.
        if row not in {sel.row() for sel in self.queue_table.selectedItems()}:
            self.queue_table.selectRow(row)
        combos = self.selected_combos()
        if not combos:
            return

        tr = self.tr
        menu = QMenu(self.queue_table)
        edit_action = menu.addAction(tr("arena.queue_edit"))
        edit_action.setEnabled(len(combos) == 1)
        copy_action = menu.addAction(tr("arena.card_copy"))
        menu.addSeparator()
        delete_action = menu.addAction(tr("arena.queue_remove"))
        delete_action.setEnabled(not self._service.is_running)

        chosen = menu.exec(self.queue_table.viewport().mapToGlobal(pos))
        if chosen is edit_action:
            self.edit_combo(combos[0])
        elif chosen is copy_action:
            self.copy_selected()
        elif chosen is delete_action:
            self.remove_selected()

    def copy_selected(self) -> bool:
        """고른 조합들의 작가 블록을 클립보드로 (여러 개면 줄바꿈으로 잇는다)."""
        combos = self.selected_combos()
        if not combos:
            self.status_message.emit(self.tr("arena.queue_no_selection"))
            return False
        blocks = [format_artist_block(combo.slots, self.arena.use_prefix) for combo in combos]
        QGuiApplication.clipboard().setText("\n".join(block for block in blocks if block))
        self.status_message.emit(self.tr("arena.copied"))
        return True

    def edit_combo(self, combo, prompt: str | None = None) -> bool:
        """조합의 작가 블록을 손으로 고친다.

        조합은 `ComboSlot`(이름+가중치)의 묶음이지만, 사용자에게는 완성 프롬프트
        문자열이 가장 익숙하다. `format_artist_block`으로 문자열을 만들어 보여 주고,
        `parse_artist_block`으로 다시 자리 목록으로 되돌린다 — 삭제 흐름과 달리 그림·
        전적은 건드리지 않는다 (작가 구성만 손보는 것이라 그대로 유효하다).

        `prompt`를 넘기면 편집 창을 띄우지 않고 그 문자열을 바로 적용한다 (테스트용).
        """
        original = format_artist_block(combo.slots, self.arena.use_prefix)
        edited = prompt if prompt is not None else _EditComboDialog.edit(self._i18n, original, self)
        if edited is None or edited.strip() == original.strip():
            return False
        slots, warnings = parse_artist_block(edited)
        if not slots:
            self.status_message.emit(self.tr("arena.queue_edit_empty"))
            return False
        combo.slots = tuple(slots)
        self._service.save()
        if warnings:
            self.status_message.emit(self.tr("arena.queue_edit_warnings").format(", ".join(warnings)))
        else:
            self.status_message.emit(self.tr("arena.queue_edited"))
        self.state_changed.emit()
        return True

    def remove_selected(self) -> int:
        """고른 조합을 큐에서 지운다. 지운 수를 돌려준다.

        그림이나 전적이 딸린 조합이 섞여 있으면 먼저 묻는다 — 큐 목록에는 아직
        안 뽑은 것과 이미 평가까지 끝난 것이 함께 있어서, 넓게 골라 지우다 애써
        모은 결과를 날리기 쉽다.
        """
        combos = self.selected_combos()
        if not combos:
            self.status_message.emit(self.tr("arena.queue_no_selection"))
            return 0
        if not self._confirm_valuable(combos):
            return 0
        return self._remove(combos)

    def _confirm_valuable(self, combos: list) -> bool:
        """그림·전적이 딸린 조합이 있으면 확인을 받는다. 없으면 그냥 지운다."""
        with_image = sum(1 for combo in combos if combo.has_image)
        rated = sum(1 for combo in combos if combo.matches)
        if not with_image and not rated:
            return True
        key = (
            "arena.queue_confirm_delete_files"
            if self.arena.delete_images_with_combo
            else "arena.queue_confirm"
        )
        answer = QMessageBox.question(
            self,
            self.tr("arena.queue_remove"),
            self.tr(key).format(len(combos), with_image, rated),
        )
        return answer == QMessageBox.StandardButton.Yes

    def remove_pending(self) -> int:
        """그림이 없는 조합을 한 번에 지운다.

        조건을 잘못 잡고 잔뜩 만들었을 때 하나씩 고르지 않아도 되게. 이미 그림을 뽑은
        조합은 건드리지 않는다 — 크레딧을 쓴 것들이다.
        """
        combos = pending_combos(self.state)
        if not combos:
            self.status_message.emit(self.tr("arena.nothing_pending"))
            return 0
        return self._remove(combos)

    def _remove(self, combos: list) -> int:
        """서비스를 거쳐 지운다 — 잘못 눌러도 `되돌리기`로 되살릴 수 있다."""
        removed = self._service.remove_combos(combos, delete_images=self.arena.delete_images_with_combo)
        self.status_message.emit(self.tr("arena.queue_removed").format(removed))
        self.state_changed.emit()
        return removed

    def undo_removal(self) -> int:
        """마지막 삭제를 되돌린다. 되살린 수를 돌려준다."""
        restored = self._service.undo_last_removal()
        if not restored:
            self.status_message.emit(self.tr("arena.undo_nothing"))
            return 0
        self.status_message.emit(self.tr("arena.undone").format(restored))
        self.state_changed.emit()
        return restored

    def _on_delete_images_toggled(self, checked: bool) -> None:
        self.arena.delete_images_with_combo = checked

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        """큐 표에서 Delete를 누르면 고른 조합을 지운다.

        F2(수정)·Ctrl+C(복사)는 `QShortcut`으로 받는다 — 표 위젯이 그 키를 먼저
        먹어(F2는 편집 시작, Ctrl+C는 뷰가 가로챈다) 여기까지 오지 않기 때문이다.
        Delete는 편집 트리거가 없어 표가 소비하지 않으므로 그대로 둔다.
        """
        if event.key() == Qt.Key.Key_Delete and self.queue_table.hasFocus():
            self.remove_selected()
            return
        super().keyPressEvent(event)

    def _build_queue_shortcuts(self) -> list[QShortcut]:
        """큐 표에 포커스가 있을 때만 사는 단축키 (F2 수정 · Ctrl+C 복사)."""
        shortcuts = []
        for sequence, handler in (
            (QKeySequence(Qt.Key.Key_F2), self._edit_focused_combo),
            (QKeySequence.StandardKey.Copy, self.copy_selected),
        ):
            shortcut = QShortcut(sequence, self.queue_table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            shortcuts.append(shortcut)
        return shortcuts

    def _edit_focused_combo(self) -> None:
        """F2 단축키 — 하나만 골랐을 때 그 조합을 고친다 (여러 개는 한 덩어리로 못 고친다)."""
        combos = self.selected_combos()
        if len(combos) == 1:
            self.edit_combo(combos[0])

    def _combo_summary(self, combo_id: str) -> str:
        """상태줄에 넣을 조합 요약 — 길면 자른다."""
        combo = self.state.combo(combo_id)
        if combo is None:
            return ""
        text = format_artist_block(combo.slots, self.arena.use_prefix)
        return text if len(text) <= _STATUS_COMBO_CHARS else text[: _STATUS_COMBO_CHARS - 1] + "…"

    def _report_finish(self, event: ArenaBatchFinished) -> None:
        if event.error:
            self.status_message.emit(self.tr("arena.generation_error").format(event.error))
        elif event.stopped:
            self.status_message.emit(self.tr("arena.generation_stopped").format(event.completed))
        else:
            self.status_message.emit(self.tr("arena.generation_done").format(event.completed))

    # ── 동작 ────────────────────────────────────────────────────────────

    def add_manual_combo(self) -> bool:
        """입력한 작가·가중치로 조합 하나를 직접 만든다. 만들었으면 True.

        편집 창과 같은 `1.2::artist:이름 ::` 문법을 받는다. 가중치 껍데기 없이 이름만
        적은 작가에게는 옆의 `기본 가중치` 값을 준다 — 하나하나 `1.0::`을 적지 않아도
        된다. 명단에 없는 작가도 그대로 쓸 수 있다. 이미 같은 작가 구성이 있으면
        (가중치만 달라도) 중복으로 보고 막는다 — 무작위 생성과 같은 규칙이다.
        """
        text = self.manual_edit.toPlainText().strip()
        if not text:
            self.status_message.emit(self.tr("arena.manual_empty"))
            return False
        slots, warnings = parse_artist_block(text)
        if not slots:
            self.status_message.emit(self.tr("arena.manual_empty"))
            return False
        # 가중치 껍데기 없이 적힌 작가는 1.0으로 파싱된다 — 그 자리에 기본 가중치를 준다.
        default_weight = normalize_weight(self.manual_weight_spin.value())
        slots = tuple(
            slot if slot.weight != 1.0 else ComboSlot(name=slot.name, weight=default_weight) for slot in slots
        )
        signature = name_signature(slots)
        if any(name_signature(combo.slots) == signature for combo in self.state.combos):
            self.status_message.emit(self.tr("arena.manual_duplicate"))
            return False
        self.state.combos.append(Combo(slots=slots, generation=1))
        self._service.save()
        self.manual_edit.clear()
        if warnings:
            self.status_message.emit(self.tr("arena.queue_edit_warnings").format(", ".join(warnings)))
        else:
            self.status_message.emit(self.tr("arena.manual_added").format(len(slots)))
        self.state_changed.emit()
        return True

    def make_combos(self) -> int:
        """무작위 조합을 `batch_size`개 만든다. 실제로 만든 수를 돌려준다."""
        self.commit()
        if not self.state.artists:
            self.status_message.emit(self.tr("arena.need_artists"))
            return 0
        existing = {name_signature(combo.slots) for combo in self.state.combos}
        made = generate_combos(self.state.artists, self.arena.combo_params(), self.arena.batch_size, existing)
        self.state.combos.extend(made)
        self._service.save()
        self.status_message.emit(self.tr("arena.combos_made").format(len(made)))
        self.state_changed.emit()
        return len(made)

    def _spec(self, request) -> ArenaSpec:
        arena = self.arena
        if arena.width > 0 and arena.height > 0:
            request = dataclasses.replace(request, width=arena.width, height=arena.height)
        return ArenaSpec(
            request=request,
            seed=arena.seed,
            insert_position=arena.insert_position,
            use_prefix=arena.use_prefix,
            image_format=self._settings.image_format,
            delay_seconds=self._settings.batch.delay_seconds,
        )

    def start_generation(self) -> int:
        """그림이 없는 조합의 그림을 뽑기 시작한다. 큐에 넣은 수를 돌려준다."""
        self.commit()
        pending = pending_combos(self.state)
        if not pending:
            self.status_message.emit(self.tr("arena.nothing_pending"))
            return 0
        spec = self._spec(self._request_provider())
        try:
            queued = self._service.start(pending, spec)
        except ArenaBusyError:
            self.status_message.emit(self.tr("arena.busy"))
            return 0
        # 뽑은 시드를 설정에 적어 둔다 — 다음에 켜도 같은 조건으로 이어 뽑는다.
        if self.arena.seed <= 0:
            self.arena.seed = self._service.session_seed
        self.refresh()  # 안내는 ArenaBatchStarted가 상태줄에 띄운다
        return queued


__all__ = ["RESOLUTION_FOLLOW", "BuildTab", "index_of_data"]
