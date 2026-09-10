"""LLM 추천 탭 — V4 참조 그림체와 후보를 로컬 VLM이 비교해 점수로 랭킹한다.

아레나의 다른 탭이 사람이 직접 고르는 이상형 월드컵이라면, 이 탭은 그 1차 선별을
로컬 VLM(LM Studio)에게 맡긴다: **V4 그림체 참조 이미지 여러 장**을 목표로 등록하고,
`실행`을 누르면 그림이 있는 후보들을 한 장씩 넣어 0~100 유사도 점수를 받는다.

사람 Elo와는 **완전히 별개**다 (`Combo.judge_score`에만 쓴다) — LLM이 추천한 순위를
보고, 마음에 들면 후속 액션으로 상위 N개를 즐겨찾기로 올려 월드컵에서 더 비교한다.

판정은 `services/style_judge_service.StyleJudgeService.run()`이 하고, 그 호출은
블로킹이라 `_JudgeWorker(QThread)` 안에서 돈다 (프롬프트 어시스턴트의 `_LLMWorker`와
같은 패턴). 연결(host)·타임아웃은 메인 설정(`settings.lmstudio`)을 따르고, 판정용
모델·참조 상한·즐겨찾기 개수는 `settings.arena`에 따로 둔다.

## 화면

참조·설정과 결과가 세로를 나눠 갖고 있었다. 참조는 왼쪽에 고정하고 랭킹에 오른쪽을
통째로 준다. 랭킹 표에는 두 가지를 더했다.

- **썸네일** — 점수만 보고는 무엇이 뽑혔는지 알 수 없다.
- **점수 막대** — 숫자만 늘어놓으면 **90점대에 몰리는 포화**가 눈에 띄지 않는다.
  판별력이 약한 모델에서 흔한 일이고, 그때가 판독 지시를 손봐야 할 때다.

참조 그림은 끌어다 놓아서도 넣는다 (작가 명단 탭과 같은 동작). 자주 쓰지 않는
`결과 저장`·`점수 비우기`는 더보기 메뉴로 접었다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import format_artist_block
from ...core.arena.models import Combo
from ...core.llm.lmstudio_client import LMStudioPromptGenerator
from ...core.llm.style_judge import DEFAULT_JUDGE_SYSTEM_PROMPT, StyleJudgeConfig
from ...services.style_judge_service import (
    JudgeFinished,
    JudgeProgress,
    JudgeStarted,
    StyleJudgeError,
    StyleJudgeService,
    apply_favorite_top_n,
    candidates_to_judge,
    clear_judge_scores,
    judge_leaderboard,
    judge_results_to_csv,
    load_reference_images,
)
from .base import ArenaTab

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_THUMB = 96  # 참조 썸네일 한 변 (논리 픽셀)
_ROW_THUMB = 44  # 랭킹 표 줄 썸네일 한 변 (논리 픽셀)

#: 좌우 패널 사이의 여백과 처음 뜰 때의 나눔 비율 (논리 픽셀).
PANEL_GAP = 8
SPLIT_SIZES = (320, 680)

#: 자주 쓰지 않는 동작을 접어 두는 더보기 버튼의 기호.
MORE_ICON = "⋯"

#: 판독 점수의 최댓값과 막대 폭 (논리 픽셀).
JUDGE_SCORE_MAX = 100
SCORE_BAR_WIDTH = 90

#: LM Studio 예외 클래스명 → 안내 문구 i18n 키. 서비스가 `error_key`로 클래스명을 준다.
_ERROR_KEYS = {
    "LMStudioNotInstalled": "lmstudio.err_not_installed",
    "LMStudioConnectionError": "lmstudio.err_connection",
    "LMStudioNoModelError": "lmstudio.err_no_model",
    "LMStudioVisionUnsupported": "lmstudio.err_vision",
    "LMStudioTimeoutError": "lmstudio.err_timeout",
    "LMStudioResponseError": "lmstudio.err_response",
}


class _JudgeWorker(QThread):
    """그림체 판독 한 번 (여러 후보를 순차로). `cancel()`로 중단.

    `StyleJudgeService.run()`을 그대로 감싸, 서비스가 내는 이벤트를 Qt 시그널로 옮긴다.
    """

    started_judging = Signal(object)  # JudgeStarted
    progressed = Signal(object)  # JudgeProgress
    finished_judging = Signal(object)  # JudgeFinished

    def __init__(
        self,
        service: StyleJudgeService,
        references: list[bytes],
        combos: list[Combo],
        config: StyleJudgeConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._references = references
        self._combos = combos
        self._config = config
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        def dispatch(event) -> None:
            if isinstance(event, JudgeStarted):
                self.started_judging.emit(event)
            elif isinstance(event, JudgeProgress):
                self.progressed.emit(event)
            elif isinstance(event, JudgeFinished):
                self.finished_judging.emit(event)

        try:
            self._service.run(
                self._references,
                self._combos,
                self._config,
                on_event=dispatch,
                should_cancel=lambda: self._cancel,
            )
        except StyleJudgeError as e:
            # 시작 자체가 안 됐다 (참조/후보 없음) — 완료 이벤트로 감싸 UI가 한 곳에서 처리.
            self.finished_judging.emit(
                JudgeFinished(completed=0, failed=0, error=str(e), error_key="StyleJudgeError")
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("style judge worker crashed")
            self.finished_judging.emit(
                JudgeFinished(completed=0, failed=0, error=str(e), error_key="Exception")
            )


class JudgeTab(ArenaTab):
    """LLM 추천 — 참조 그림체와 닮은 후보를 자동으로 점수화해 랭킹한다."""

    KEY = "judge"

    #: 랭킹 표 컬럼. 썸네일과 점수 막대가 앞에 붙는다 — 점수만 보고는 무엇이
    #: 뽑혔는지 알 수 없고, 숫자만으로는 90점대 포화가 눈에 띄지 않는다.
    _COL_THUMBNAIL = 0
    _COL_RANK = 1
    _COL_SCORE = 2
    _COL_BAR = 3
    _COL_COMBO = 4
    _COL_REASON = 5
    _COLUMNS = 6

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)
        self._generator = LMStudioPromptGenerator()
        # 아레나와 store·state를 공유한다 — 점수도 같은 arena.json에 남는다.
        self._judge_service = StyleJudgeService(service.store, service.state, self._generator)
        self._worker: _JudgeWorker | None = None

        root = QVBoxLayout(self)
        # 참조·설정과 결과가 세로를 나눠 갖고 있었다 — 참조는 왼쪽에 고정하고
        # 랭킹에 오른쪽을 통째로 준다.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, 1)

        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, PANEL_GAP, 0)

        # ── 참조 이미지 ─────────────────────────────────────────────────
        self.ref_label = QLabel()
        layout.addWidget(self.ref_label)
        self.ref_list = QListWidget()
        self.ref_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.ref_list.setIconSize(QSize(_THUMB, _THUMB))
        self.ref_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.ref_list.setMinimumHeight(_THUMB + 44)
        self.ref_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.ref_list)

        ref_btns = QHBoxLayout()
        self.add_ref_button = QPushButton()
        self.add_ref_button.clicked.connect(self._on_add_references)
        ref_btns.addWidget(self.add_ref_button)
        self.remove_ref_button = QPushButton()
        self.remove_ref_button.clicked.connect(self._on_remove_references)
        ref_btns.addWidget(self.remove_ref_button)
        self.clear_ref_button = QPushButton()
        self.clear_ref_button.clicked.connect(self._on_clear_references)
        ref_btns.addWidget(self.clear_ref_button)
        ref_btns.addStretch(1)
        layout.addLayout(ref_btns)

        # ── 판독 지시(시스템 프롬프트) — 접이식 ──────────────────────────
        #
        # 모델마다 잘 듣는 채점 지시가 다르다. 특히 판별력이 약한 모델은 기본 프롬프트로도
        # 점수가 90~100에 몰리므로(포화), 여기서 rubric을 직접 손봐 실험할 수 있게 한다.
        # 비워 두면 내장 기본 프롬프트를 쓴다 — placeholder로 그 사실을 알린다.
        self.prompt_group = QGroupBox()
        self.prompt_group.setCheckable(True)
        self.prompt_group.setChecked(False)  # 기본은 접힘 — 대개 손댈 필요가 없다
        self.prompt_group.toggled.connect(self._on_prompt_group_toggled)
        group_layout = QVBoxLayout(self.prompt_group)
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setFixedHeight(150)
        group_layout.addWidget(self.prompt_edit)
        prompt_btns = QHBoxLayout()
        self.prompt_hint_label = QLabel()
        self.prompt_hint_label.setWordWrap(True)
        prompt_btns.addWidget(self.prompt_hint_label, 1)
        self.prompt_reset_button = QPushButton()
        self.prompt_reset_button.clicked.connect(self._on_reset_prompt)
        prompt_btns.addWidget(self.prompt_reset_button)
        group_layout.addLayout(prompt_btns)
        layout.addWidget(self.prompt_group)
        self._prompt_body = (self.prompt_edit, self.prompt_hint_label, self.prompt_reset_button)

        # ── 실행 줄 ─────────────────────────────────────────────────────
        run_row = QHBoxLayout()
        self.run_button = QPushButton()
        self.run_button.clicked.connect(self._on_run)
        run_row.addWidget(self.run_button)
        self.stop_button = QPushButton()
        self.stop_button.clicked.connect(self._on_stop)
        self.stop_button.setEnabled(False)
        run_row.addWidget(self.stop_button)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFormat("%v / %m")
        run_row.addWidget(self.progress, 1)
        layout.addLayout(run_row)

        # 결과 CSV에 판독 조건을 함께 담을지. 모델을 바꿔 가며 견주는 사람에게는
        # 점수만 있는 CSV가 나중에 "어느 모델로 뽑은 거였지"가 되어 버린다.
        self.run_info_check = QCheckBox()
        self.run_info_check.setChecked(self.arena.judge_export_run_info)
        layout.addWidget(self.run_info_check)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        self.splitter.addWidget(left)

        # ── 오른쪽: 랭킹 ────────────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(PANEL_GAP, 0, 0, 0)

        self.table = QTableWidget(0, self._COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(_ROW_THUMB + 8)
        self.table.setIconSize(QSize(_ROW_THUMB, _ROW_THUMB))
        header = self.table.horizontalHeader()
        for col in (self._COL_THUMBNAIL, self._COL_RANK, self._COL_SCORE, self._COL_BAR):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_COMBO, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_REASON, QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.table, 1)

        # ── 후속 액션 ───────────────────────────────────────────────────
        action_row = QHBoxLayout()
        self.top_n_label = QLabel()
        action_row.addWidget(self.top_n_label)
        self.top_n_spin = QSpinBox()
        self.top_n_spin.setRange(1, 100)
        self.top_n_spin.setValue(max(1, self.arena.judge_favorite_top_n))
        action_row.addWidget(self.top_n_spin)
        self.favorite_button = QPushButton()
        self.favorite_button.clicked.connect(self._on_favorite_top_n)
        action_row.addWidget(self.favorite_button)
        self.send_button = QPushButton()
        self.send_button.clicked.connect(self._on_send_selected)
        action_row.addWidget(self.send_button)
        action_row.addStretch(1)
        # 결과 저장(CSV)과 점수 비우기는 자주 쓰지 않는다 — 늘 자리를 차지하는 대신
        # 더보기 메뉴로 접는다. 버튼 자체는 그대로라 동작과 검증은 달라지지 않는다.
        self.export_button = QPushButton()
        self.export_button.clicked.connect(self._on_export_csv)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self._on_clear_scores)
        self.more_button = QPushButton(MORE_ICON)
        self.more_menu = QMenu(self)
        # 메뉴 항목은 숨은 버튼을 대신 누르지 않고 **같은 핸들러를 직접** 부른다.
        # 버튼은 켜짐/꺼짐 상태를 들고 있는 자리로만 남는다 (꺼진 버튼을 대신 누르면
        # 아무 일도 일어나지 않으면서 메뉴는 멀쩡해 보인다).
        self.export_action = self.more_menu.addAction("")
        self.export_action.triggered.connect(self._on_export_csv)
        self.clear_action = self.more_menu.addAction("")
        self.clear_action.triggered.connect(self._on_clear_scores)
        self.more_button.setMenu(self.more_menu)
        action_row.addWidget(self.more_button)
        right_layout.addLayout(action_row)
        self.splitter.addWidget(right)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes(list(SPLIT_SIZES))

        # 참조도 끌어다 놓아 넣는다 — 작가 명단 탭과 같은 동작.
        self.setAcceptDrops(True)

        # 저장된 커스텀 프롬프트를 복원한다. 값이 있으면 그룹을 펼쳐 바로 보이게 한다.
        saved_prompt = self.arena.judge_system_prompt
        self.prompt_edit.setPlainText(saved_prompt)
        self.prompt_group.setChecked(bool(saved_prompt.strip()))
        self._apply_prompt_group_state()

        self.retranslate()

    # ── 바깥 계약 ───────────────────────────────────────────────────────

    def refresh(self) -> None:
        """상태 → 화면. 판독 중에는 표를 건드리지 않는다 (진행 이벤트가 채운다)."""
        self._judge_service.set_state(self.state)
        if self._is_busy():
            return
        self._reload_references()
        self._reload_table()
        self._update_buttons()

    def commit(self) -> None:
        """참조 목록·즐겨찾기 개수·커스텀 프롬프트를 설정에 되쓴다."""
        self.arena.judge_reference_paths = self._reference_paths()
        self.arena.judge_favorite_top_n = self.top_n_spin.value()
        self.arena.judge_system_prompt = self.prompt_edit.toPlainText().strip()
        self.arena.judge_export_run_info = self.run_info_check.isChecked()

    def retranslate(self) -> None:
        tr = self.tr
        self.ref_label.setText(tr("arena.judge_references"))
        self.add_ref_button.setText(tr("arena.judge_add_ref"))
        self.remove_ref_button.setText(tr("arena.judge_remove_ref"))
        self.clear_ref_button.setText(tr("arena.judge_clear_ref"))
        self.run_button.setText(tr("arena.judge_run"))
        self.stop_button.setText(tr("arena.judge_stop"))
        self.top_n_label.setText(tr("arena.judge_top_n"))
        self.favorite_button.setText(tr("arena.judge_favorite_top"))
        self.send_button.setText(tr("arena.judge_send_selected"))
        self.export_button.setText(tr("arena.judge_export"))
        self.export_button.setToolTip(tr("arena.judge_export_hint"))
        self.clear_button.setText(tr("arena.judge_clear"))
        self.clear_button.setToolTip(tr("arena.judge_clear_hint"))
        self.export_action.setText(tr("arena.judge_export"))
        self.clear_action.setText(tr("arena.judge_clear"))
        self.more_button.setToolTip(tr("arena.judge_more"))
        self.run_info_check.setText(tr("arena.judge_export_run_info"))
        self.run_info_check.setToolTip(tr("arena.judge_export_run_info_hint"))
        self.prompt_group.setTitle(tr("arena.judge_prompt_group"))
        self.prompt_group.setToolTip(tr("arena.judge_prompt_hint"))
        self.prompt_edit.setPlaceholderText(tr("arena.judge_prompt_placeholder"))
        self.prompt_hint_label.setText(tr("arena.judge_prompt_hint"))
        self.prompt_reset_button.setText(tr("arena.judge_prompt_reset"))
        self.table.setHorizontalHeaderLabels(
            [
                "",  # 썸네일 — 머리글을 붙이면 그림보다 글이 넓어진다
                tr("arena.judge_col_rank"),
                tr("arena.judge_col_score"),
                "",  # 점수 막대 — 바로 왼쪽 칸이 이미 `점수`다
                tr("arena.judge_col_combo"),
                tr("arena.judge_col_reason"),
            ]
        )

    def on_arena_event(self, event) -> None:
        """아레나 이벤트는 판독과 무관하다 — 새 그림이 생기면 후보 수만 다시 센다."""
        if not self._is_busy():
            self._update_buttons()

    # ── 참조 이미지 ─────────────────────────────────────────────────────

    def _reload_references(self) -> None:
        """설정의 참조 경로로 목록을 다시 채운다."""
        self.ref_list.clear()
        for path in self.arena.judge_reference_paths:
            self._add_reference_item(path)

    def _add_reference_item(self, path: str) -> None:
        item = QListWidgetItem(Path(path).name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            item.setIcon(QIcon(pixmap))
        self.ref_list.addItem(item)

    def _reference_paths(self) -> list[str]:
        return [self.ref_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.ref_list.count())]

    def _on_add_references(self) -> None:
        tr = self.tr
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("arena.judge_choose_ref"), "", tr("assistant.image_filter")
        )
        existing = set(self._reference_paths())
        for path in paths:
            if path not in existing and Path(path).suffix.lower() in _IMAGE_SUFFIXES:
                self._add_reference_item(path)
                existing.add(path)
        self.commit()
        self._update_buttons()

    def _on_remove_references(self) -> None:
        for item in self.ref_list.selectedItems():
            self.ref_list.takeItem(self.ref_list.row(item))
        self.commit()
        self._update_buttons()

    def _on_clear_references(self) -> None:
        self.ref_list.clear()
        self.commit()
        self._update_buttons()

    # ── 끌어다 놓기 ─────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt 콜백 이름)
        if self._dropped_images(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """참조 그림을 끌어다 놓아 넣는다 — 작가 명단 탭과 같은 동작.

        판독 중에는 받지 않는다. 돌고 있는 판독은 시작할 때의 참조 목록을 이미 들고
        있어, 여기서 바꿔 봐야 이번 판독에는 반영되지 않는다.
        """
        paths = self._dropped_images(event)
        if not paths or self._is_busy():
            return
        event.acceptProposedAction()
        existing = set(self._reference_paths())
        added = 0
        for path in paths:
            if path not in existing:
                self._add_reference_item(path)
                existing.add(path)
                added += 1
        self.commit()
        self._update_buttons()
        self.status_label.setText(self.tr("arena.judge_ref_dropped").format(added))

    @staticmethod
    def _dropped_images(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in _IMAGE_SUFFIXES:
                paths.append(local)
        return paths

    # ── 판독 지시(시스템 프롬프트) ──────────────────────────────────────

    def _on_prompt_group_toggled(self, _checked: bool) -> None:
        self._apply_prompt_group_state()

    def _apply_prompt_group_state(self) -> None:
        """접힘/펼침에 따라 내부 위젯을 숨기거나 보인다.

        `QGroupBox.setCheckable`은 체크를 꺼도 자식을 비활성화만 하고 숨기지는 않는다.
        그러면 접어도 자리를 그대로 차지해 접는 의미가 없으므로, 직접 visibility를 준다.
        """
        expanded = self.prompt_group.isChecked()
        for widget in self._prompt_body:
            widget.setVisible(expanded)

    def _on_reset_prompt(self) -> None:
        """편집칸을 내장 기본 프롬프트로 채운다 — 실험하다 되돌리고 싶을 때.

        기본을 **보여 주고** 편집할 수 있게, 빈칸이 아니라 기본 문안을 넣는다. 그대로
        두고 판독해도 되고, 다시 비우면 판독 시 자동으로 기본이 쓰인다(같은 결과).
        """
        self.prompt_edit.setPlainText(DEFAULT_JUDGE_SYSTEM_PROMPT)
        self.commit()

    # ── 실행 ────────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        tr = self.tr
        if self._is_busy():
            return
        paths = self._reference_paths()
        if not paths:
            self.status_label.setText(tr("arena.judge_need_ref"))
            return
        references = load_reference_images(paths, self.arena.judge_max_references)
        if not references:
            self.status_label.setText(tr("arena.judge_ref_load_failed"))
            return
        combos = candidates_to_judge(self.state)
        if not combos:
            self.status_label.setText(tr("arena.judge_no_candidates"))
            return

        dropped = len(paths) - len(references)
        if dropped > 0:
            self.status_label.setText(tr("arena.judge_ref_capped").format(len(references), dropped))

        config = StyleJudgeConfig(
            host=self._settings.lmstudio.host,
            model=self.arena.judge_model,
            timeout=self._settings.lmstudio.timeout_seconds,
            max_reference_images=self.arena.judge_max_references,
            system_prompt=self.prompt_edit.toPlainText().strip(),
        )
        self._judge_service.set_state(self.state)
        self.table.setRowCount(0)
        self._set_busy(True)
        self.progress.setRange(0, len(combos))
        self.progress.setValue(0)

        worker = _JudgeWorker(self._judge_service, references, combos, config, self)
        worker.started_judging.connect(self._on_started)
        worker.progressed.connect(self._on_progress)
        worker.finished_judging.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def _on_stop(self) -> None:
        if self._worker is not None:
            self.status_label.setText(self.tr("arena.judge_stopping"))
            self.stop_button.setEnabled(False)
            self._worker.cancel()

    def _on_started(self, event: JudgeStarted) -> None:
        self.status_label.setText(self.tr("arena.judge_started").format(event.total, event.references))

    def _on_progress(self, event: JudgeProgress) -> None:
        self.progress.setValue(event.index)
        self.status_label.setText(
            self.tr("arena.judge_progress").format(event.index, event.total, event.score)
        )
        # 표는 완료 후 한 번에 정렬해 그린다 — 매 건 삽입 정렬하면 순위가 계속 바뀌어
        # 눈이 어지럽다. 진행 표시는 상태줄과 진행 막대로 충분하다.

    def _on_finished(self, event: JudgeFinished) -> None:
        tr = self.tr
        self._set_busy(False)
        self._worker = None
        self._reload_table()
        self._update_buttons()
        self.state_changed.emit()  # 다른 탭(월드컵 등)도 즐겨찾기 등 반영
        if event.error:
            key = _ERROR_KEYS.get(event.error_key or "", "lmstudio.err_response")
            self.status_label.setText(tr(key, event.error))
        elif event.stopped:
            self.status_label.setText(
                self._with_model(tr("arena.judge_stopped").format(event.completed, event.failed), event.model)
            )
        else:
            self.status_label.setText(
                self._with_model(tr("arena.judge_done").format(event.completed, event.failed), event.model)
            )

    def _with_model(self, text: str, model: str) -> str:
        """끝난 뒤에는 **어느 모델이 매겼는지**까지 말한다.

        설정에 적어 둔 이름이 아니라 서버가 실제로 고른 이름이다 — 자동 선택이거나
        부분 일치로 골라졌을 수 있어, 모델을 바꿔 가며 견주는 사람에게는 이쪽이 맞다.
        """
        if not model:
            return text
        return f"{text} — {self.tr('arena.judge_by_model').format(model)}"

    # ── 표 ──────────────────────────────────────────────────────────────

    def _reload_table(self) -> None:
        ranked = judge_leaderboard(self.state)
        use_prefix = self.arena.use_prefix
        self.table.setRowCount(len(ranked))
        for row, combo in enumerate(ranked):
            self._set_thumbnail(row, combo)
            self._set_cell(row, self._COL_RANK, str(row + 1))
            self._set_cell(row, self._COL_SCORE, str(combo.judge_score))
            self._set_score_bar(row, combo.judge_score)
            combo_item = self._set_cell(row, self._COL_COMBO, format_artist_block(combo.slots, use_prefix))
            combo_item.setData(Qt.ItemDataRole.UserRole, combo.id)  # 후속 액션이 조합을 찾을 열쇠
            self._set_cell(row, self._COL_REASON, combo.judge_reason)

    def _set_cell(self, row: int, col: int, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        self.table.setItem(row, col, item)
        return item

    def _set_thumbnail(self, row: int, combo: Combo) -> None:
        """줄마다 그 조합의 그림 — 점수만 보고는 무엇이 뽑혔는지 알 수 없다."""
        item = QTableWidgetItem()
        path = self._service.store.image_path(combo)
        if path is not None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
        self.table.setItem(row, self._COL_THUMBNAIL, item)

    def _set_score_bar(self, row: int, score: int) -> None:
        """점수를 막대로도 보여 준다.

        숫자만 늘어놓으면 **90점대에 몰리는 포화**가 눈에 띄지 않는다. 판별력이 약한
        모델에서 흔한 일이고, 그때가 판독 지시를 손봐야 할 때다 — 막대가 나란히 꽉
        차 있으면 한눈에 보인다. 숫자는 옆 칸에 그대로 있어 읽거나 복사할 수 있다.
        """
        bar = QProgressBar()
        bar.setRange(0, JUDGE_SCORE_MAX)
        bar.setValue(max(0, min(JUDGE_SCORE_MAX, score)))
        bar.setTextVisible(False)
        bar.setFixedWidth(SCORE_BAR_WIDTH)
        self.table.setCellWidget(row, self._COL_BAR, bar)

    # ── 후속 액션 ───────────────────────────────────────────────────────

    def _on_favorite_top_n(self) -> None:
        newly = apply_favorite_top_n(self.state, self.top_n_spin.value())
        self._service.save()
        self.state_changed.emit()
        self.status_label.setText(self.tr("arena.judge_favorited").format(newly))

    def _on_send_selected(self) -> None:
        """선택한 조합의 작가 블록을 클립보드로 — 메인 프롬프트에 바로 붙일 수 있게."""
        combo = self._selected_combo()
        if combo is None:
            self.status_label.setText(self.tr("arena.judge_select_row"))
            return
        block = format_artist_block(combo.slots, self.arena.use_prefix)
        QGuiApplication.clipboard().setText(block)
        self.status_label.setText(self.tr("arena.judge_copied"))

    def _selected_combo(self) -> Combo | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), self._COL_COMBO)
        if item is None:
            return None
        combo_id = item.data(Qt.ItemDataRole.UserRole)
        return self.state.combo(combo_id) if combo_id else None

    def _on_export_csv(self) -> None:
        """판독 랭킹을 CSV 파일로 저장한다 — 표에 보이는 순서 그대로.

        아레나 기록 내보내기(zip)와 다르다. 이건 사람이 읽거나 스프레드시트로 옮기는
        **결과물**이라, 다시 불러오지는 않는다.
        """
        tr = self.tr
        if not judge_leaderboard(self.state):
            self.status_label.setText(tr("arena.judge_export_none"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("arena.judge_choose_csv"), self._suggested_csv_name(), tr("arena.judge_csv_filter")
        )
        if not path:
            return
        text = judge_results_to_csv(
            self.state, self.arena.use_prefix, include_run=self.run_info_check.isChecked()
        )
        try:
            # BOM(utf-8-sig)을 붙인다 — Excel이 UTF-8 CSV를 열 때 한글·근거 텍스트가
            # 깨지지 않게 한다. 표준 CSV 리더는 BOM을 무시한다.
            Path(path).write_text(text, encoding="utf-8-sig", newline="")
        except OSError as e:
            logger.warning("could not write judge CSV %s: %s", path, e)
            self.status_label.setText(tr("arena.judge_export_failed").format(e))
            return
        self.status_label.setText(tr("arena.judge_exported").format(Path(path).name))

    def _suggested_csv_name(self) -> str:
        """`llm-judge-20260908-1430.csv` — 여러 벌을 남겨도 언제 것인지 보이게."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return f"llm-judge-{stamp}.csv"

    def _on_clear_scores(self) -> None:
        """판독 점수·근거를 모두 비운다 (확인 후). 사람 Elo·전적은 건드리지 않는다.

        되돌리기가 없으므로 먼저 묻는다. 참조 이미지 등록 목록은 그대로 둬, 비운 뒤
        바로 다시 판독할 수 있게 한다.
        """
        tr = self.tr
        scored = len(judge_leaderboard(self.state))
        if not scored:
            self.status_label.setText(tr("arena.judge_clear_none"))
            return
        answer = QMessageBox.question(
            self, tr("arena.judge_clear"), tr("arena.judge_clear_confirm").format(scored)
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        cleared = clear_judge_scores(self.state)
        self._service.save()
        self._reload_table()
        self._update_buttons()
        self.state_changed.emit()
        self.status_label.setText(tr("arena.judge_cleared").format(cleared))

    # ── 상태 ────────────────────────────────────────────────────────────

    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.run_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        for widget in (
            self.add_ref_button,
            self.remove_ref_button,
            self.clear_ref_button,
            self.favorite_button,
            self.send_button,
            self.export_button,
            self.clear_button,
            self.ref_list,
            self.prompt_group,
            self.more_button,
            self.run_info_check,
        ):
            widget.setEnabled(not busy)
        self._sync_menu_actions()

    def _update_buttons(self) -> None:
        has_ref = self.ref_list.count() > 0
        has_candidates = len(candidates_to_judge(self.state)) > 0
        self.run_button.setEnabled(has_ref and has_candidates and not self._is_busy())
        has_scores = len(judge_leaderboard(self.state)) > 0
        self.favorite_button.setEnabled(has_scores and not self._is_busy())
        self.send_button.setEnabled(has_scores and not self._is_busy())
        self.export_button.setEnabled(has_scores and not self._is_busy())
        self.clear_button.setEnabled(has_scores and not self._is_busy())
        self._sync_menu_actions()

    def _sync_menu_actions(self) -> None:
        """더보기 메뉴가 버튼의 켜짐/꺼짐을 그대로 따라간다."""
        self.export_action.setEnabled(self.export_button.isEnabled())
        self.clear_action.setEnabled(self.clear_button.isEnabled())

    # ── 종료 정리 ───────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """창이 닫힐 때 워커를 정리한다 (다이얼로그가 부른다)."""
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(6000)
            self._worker = None


__all__ = ["JudgeTab"]
