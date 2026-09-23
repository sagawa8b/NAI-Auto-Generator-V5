"""결과 탭 — 순위표 하나에서 고르고, 보내고, 교배하고, 정리한다.

예전에는 `통계`와 `그림체 개선`이 **같은 데이터를 두 화면에서** 보고 있었다. 개선 탭의
"부모 후보(전적이 있거나 잠근 조합 중 상위)"는 통계 탭 티어 시트의 상위 N개와 사실상
같은 목록이라, 마음에 드는 조합을 찾아 놓고 교배하려면 탭을 옮겨 그 조합을 다시
찾아야 했다. 게다가 통계 탭 하나에 네 가지 일(티어 시트 · 결산 · 기록 내보내기 ·
작가 순위)이 세로로 겹쳐 있어 표 두 개가 서로 자리를 뺏었다.

한 화면으로 합치고 좌우로 나눈다.

- **왼쪽은 순위표 하나** — 이것이 티어 시트이자 부모 후보 목록이다. 위에 티어·세대
  분포 막대가 붙어, 어느 쪽으로 쏠렸는지 숫자를 읽지 않고 훑을 수 있다.
- **오른쪽은 고른 조합** — 그림과 프롬프트, 전적. 통계 탭에서는 태그 문자열만 보여
  "이게 어떤 그림이었지"를 알 수 없었다. 아래로 교배 · 결산 · 정리 · 기록 · 작가
  순위를 접이식으로 쌓아, 필요할 때만 편다.

되돌릴 수 없는 것(초기화·정리)은 맨 아래 한자리에 모아 위험 등급을 준다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core.arena.archive import ARCHIVE_SUFFIX, ArchiveError, ArchiveSummary, read_summary
from ...core.arena.combos import format_artist_block
from ...core.arena.elo import (
    TIERS,
    artist_ranking,
    is_settled,
    leaderboard,
    tier_counts,
    tier_of,
)
from ...core.arena.evolution import (
    breed,
    combos_with_artist,
    purge_candidates,
    select_parents,
)
from ...core.arena.finale import (
    MAX_PER_COMBO,
    MAX_TOP_N,
    finale_combos,
    finale_total,
    judge_leaderboard_combos,
)
from ...core.arena.models import INSERT_POSITIONS, Combo
from ..widgets.collapsible_section import CollapsibleSection
from ..widgets.zoomable_image_view import ZoomableImageView
from .base import ArenaTab
from .segment_bar import SegmentBar
from .style import mark_danger, mark_primary, tier_color
from .thumbnails import combo_icon, combo_pixmap

logger = logging.getLogger(__name__)

COL_TIER = 0
COL_GENERATION = 1
COL_ELO = 2
COL_JUDGE = 3  # LLM 판독 점수 (사람 Elo와 별개)
COL_RECORD = 4
COL_ARTISTS = 5
SHEET_COLUMNS = 6

#: 작가 순위에 보여 줄 인원.
ARTIST_TOP_N = 20

#: 티어 시트 정렬 기준.
SORT_ELO = "elo"  # 사람 Elo 순 (기본)
SORT_JUDGE = "judge"  # LLM 판독 점수 순

#: 부모 후보 목록의 썸네일 크기 (논리 픽셀).
THUMBNAIL_SIZE = 72

#: 고른 조합의 그림을 보여 줄 처음 높이 (논리 픽셀). 이후엔 스플리터로 조절한다.
PREVIEW_HEIGHT = 220

#: 오른쪽 패널의 그림 ↔ 아래(프롬프트·섹션)를 나누는 세로 스플리터의 처음 크기.
DETAIL_SPLIT_SIZES = (PREVIEW_HEIGHT, 300)

#: 좌우 패널 사이의 여백과 처음 뜰 때의 나눔 비율.
PANEL_GAP = 8
SPLIT_SIZES = (620, 380)


class ResultTab(ArenaTab):
    """순위표 · 고른 조합 · 교배 · 결산 · 정리 · 기록."""

    KEY = "result"

    #: 고른 조합을 메인 창 프롬프트로 (다이얼로그가 밖으로 넘긴다).
    prompt_selected = Signal(str)

    #: 결산 생성 요청 — (조합 목록, 조합당 장수). 메인 창이 자기 생성 경로로 돌린다.
    finale_requested = Signal(list, int)

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)

        root = QVBoxLayout(self)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, 1)

        self.splitter.addWidget(self._build_sheet_panel())
        self.splitter.addWidget(self._build_detail_panel(i18n))
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes(list(SPLIT_SIZES))

        self._load_settings()
        self.retranslate()

    # ── 화면 구성 ───────────────────────────────────────────────────────

    def _build_sheet_panel(self) -> QWidget:
        """왼쪽 — 분포 막대와 순위표. 이 표가 곧 부모 후보 목록이다."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, PANEL_GAP, 0)

        self.tier_label = QLabel()
        self.tier_label.setWordWrap(True)
        layout.addWidget(self.tier_label)
        self.tier_bar = SegmentBar()
        layout.addWidget(self.tier_bar)

        self.generations_label = QLabel()
        self.generations_label.setWordWrap(True)
        layout.addWidget(self.generations_label)
        self.generation_bar = SegmentBar()
        layout.addWidget(self.generation_bar)

        sort_row = QHBoxLayout()
        self.sort_label = QLabel()
        sort_row.addWidget(self.sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("", SORT_ELO)
        self.sort_combo.addItem("", SORT_JUDGE)
        self.sort_combo.currentIndexChanged.connect(self._refresh_sheet)
        sort_row.addWidget(self.sort_combo)
        sort_row.addStretch(1)
        layout.addLayout(sort_row)

        self.sheet = QTableWidget(0, SHEET_COLUMNS)
        self.sheet.verticalHeader().setVisible(False)
        self.sheet.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sheet.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sheet.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sheet.itemSelectionChanged.connect(self._refresh_detail)
        header = self.sheet.horizontalHeader()
        header.setSectionResizeMode(COL_ARTISTS, QHeaderView.ResizeMode.Stretch)
        for col in (COL_TIER, COL_GENERATION, COL_ELO, COL_JUDGE, COL_RECORD):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.sheet, 1)
        return panel

    def _build_detail_panel(self, i18n) -> QWidget:
        """오른쪽 — 고른 조합과, 그 조합으로 할 수 있는 일들.

        위는 그림, 아래는 프롬프트·전적과 다섯 섹션이다. 둘 사이를 세로 스플리터로
        나눠(프롬프트↔캐릭터 프롬프트와 같은 방식) 드래그로 그림 영역 크기를 조절하고,
        아래쪽은 따로 스크롤한다 — 섹션을 여러 개 펴도 그림이 밀려 사라지지 않는다.
        """
        # 아래쪽 — 프롬프트·전적·다섯 섹션. 좁은 창에서 넘칠 수 있어 스크롤 영역에 담는다.
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, PANEL_GAP, 0, 0)

        self.detail_label = QLabel()
        self.detail_label.setTextFormat(Qt.TextFormat.RichText)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.detail_prompt = QLabel()
        self.detail_prompt.setWordWrap(True)
        self.detail_prompt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_prompt)

        actions = QHBoxLayout()
        self.send_button = QPushButton()
        mark_primary(self.send_button)
        self.send_button.clicked.connect(self.send_selected_to_prompt)
        actions.addWidget(self.send_button, 1)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self.copy_selected)
        actions.addWidget(self.copy_button)
        layout.addLayout(actions)

        for section in (
            self._build_breed_section(i18n),
            self._build_finale_section(i18n),
            self._build_purge_section(i18n),
            self._build_archive_section(i18n),
            self._build_artists_section(i18n),
        ):
            layout.addWidget(section)
        layout.addStretch(1)

        lower_scroll = QScrollArea()
        lower_scroll.setWidgetResizable(True)
        lower_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        lower_scroll.setWidget(inner)

        # 그림 ↔ 그 아래를 드래그로 나눈다 (프롬프트↔캐릭터 프롬프트와 같은 세로 스플리터).
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_preview_section())
        split.addWidget(lower_scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes(list(DETAIL_SPLIT_SIZES))

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(PANEL_GAP, 0, 0, 0)
        panel_layout.addWidget(split)
        return panel

    def _build_preview_section(self) -> QWidget:
        """고른 조합의 그림 — 테두리로 아래 섹션들과 구분하고, 확대/축소를 붙인다.

        예전에는 높이가 고정된 라벨에 세로로 긴 그림을 우겨넣어 위아래가 잘렸다.
        여기서는 뷰포트에 맞춰(비율 유지) 보여 주므로 잘리지 않고, 휠이나 버튼으로
        확대하면 스크롤바가 생겨 원본 화질로 자세히 볼 수 있다.
        """
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # 좁은 min_size — 여기 미리보기는 개선 탭 카드만큼 작아도 된다.
        # 최소 높이는 낮게 둔다 — 실제 크기는 세로 스플리터로 조절하므로, 여기서 크게
        # 잡으면 스플리터를 위로 줄여도 그림이 더 안 줄어든다.
        self.detail_image = ZoomableImageView(min_size=120)
        self.detail_image.setMinimumHeight(80)
        outer.addWidget(self.detail_image, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addStretch(1)
        self.zoom_out_button = QToolButton()
        self.zoom_out_button.setText("\u2212")  # 뺄셈 기호 — 하이픈보다 또렷하다
        self.zoom_out_button.clicked.connect(self._zoom_preview_out)
        controls.addWidget(self.zoom_out_button)
        self.zoom_fit_button = QToolButton()
        self.zoom_fit_button.clicked.connect(self._zoom_preview_fit)
        controls.addWidget(self.zoom_fit_button)
        self.zoom_in_button = QToolButton()
        self.zoom_in_button.setText("+")
        self.zoom_in_button.clicked.connect(self._zoom_preview_in)
        controls.addWidget(self.zoom_in_button)
        outer.addLayout(controls)

        self._refresh_zoom_controls()
        return frame

    def _zoom_preview_in(self) -> None:
        self.detail_image.zoom_in()
        self._refresh_zoom_controls()

    def _zoom_preview_out(self) -> None:
        self.detail_image.zoom_out()
        self._refresh_zoom_controls()

    def _zoom_preview_fit(self) -> None:
        self.detail_image.fit()
        self._refresh_zoom_controls()

    def _refresh_zoom_controls(self) -> None:
        """확대/축소 버튼의 켜짐 여부를 현재 상태에 맞춘다."""
        self.zoom_in_button.setEnabled(self.detail_image.can_zoom_in())
        self.zoom_out_button.setEnabled(self.detail_image.can_zoom_out())
        self.zoom_fit_button.setEnabled(self.detail_image.can_zoom_out())

    def _build_breed_section(self, i18n) -> CollapsibleSection:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        self.parents_label = QLabel()
        self.parents_label.setWordWrap(True)
        layout.addWidget(self.parents_label)
        self.parents_list = QListWidget()
        self.parents_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.parents_list.setMaximumHeight(THUMBNAIL_SIZE * 2)
        layout.addWidget(self.parents_list)

        form = QFormLayout()
        self.children_spin = QSpinBox()
        self.children_spin.setRange(1, 100)
        self.children_label = QLabel()
        form.addRow(self.children_label, self.children_spin)
        self.pool_spin = QSpinBox()
        self.pool_spin.setRange(2, 100)
        self.pool_spin.valueChanged.connect(self._refresh_parents)
        self.pool_label = QLabel()
        form.addRow(self.pool_label, self.pool_spin)
        self.mutation_spin = QDoubleSpinBox()
        self.mutation_spin.setRange(0.0, 1.0)
        self.mutation_spin.setSingleStep(0.05)
        self.mutation_label = QLabel()
        form.addRow(self.mutation_label, self.mutation_spin)
        self.jitter_spin = QDoubleSpinBox()
        self.jitter_spin.setRange(0.0, 1.0)
        self.jitter_spin.setSingleStep(0.05)
        self.jitter_label = QLabel()
        form.addRow(self.jitter_label, self.jitter_spin)
        layout.addLayout(form)

        self.breed_button = QPushButton()
        self.breed_button.clicked.connect(self.breed_children)
        layout.addWidget(self.breed_button)
        # 왜 눌리지 않는지 화면이 말한다 — 예전에는 비활성인 채로 침묵했다.
        self.breed_hint = QLabel()
        self.breed_hint.setWordWrap(True)
        self.breed_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.breed_hint)

        self.breed_group = CollapsibleSection(i18n, "arena.breed_group")
        self.breed_group.set_content(body)
        return self.breed_group

    def _build_finale_section(self, i18n) -> CollapsibleSection:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.finale_top_spin = QSpinBox()
        self.finale_top_spin.setRange(1, MAX_TOP_N)
        self.finale_top_spin.valueChanged.connect(self._refresh_finale_hint)
        self.finale_top_label = QLabel()
        form.addRow(self.finale_top_label, self.finale_top_spin)
        self.finale_per_spin = QSpinBox()
        self.finale_per_spin.setRange(1, MAX_PER_COMBO)
        self.finale_per_spin.valueChanged.connect(self._refresh_finale_hint)
        self.finale_per_label = QLabel()
        form.addRow(self.finale_per_label, self.finale_per_spin)
        layout.addLayout(form)

        self.finale_unrated_check = QCheckBox()
        self.finale_unrated_check.toggled.connect(self._refresh_finale_hint)
        layout.addWidget(self.finale_unrated_check)
        self.finale_judge_check = QCheckBox()
        self.finale_judge_check.toggled.connect(self._on_finale_judge_toggled)
        layout.addWidget(self.finale_judge_check)

        # 작가를 프롬프트 어디에 끼울지 — 조합 생성 탭과 같은 옵션이다. 여기서 고른 값이
        # `arena.insert_position`으로 저장돼 결산 생성에도 그대로 쓰인다 (예전엔 늘 맨 앞).
        place_row = QHBoxLayout()
        self.finale_insert_label = QLabel()
        place_row.addWidget(self.finale_insert_label)
        self.finale_insert_combo = QComboBox()
        for position in INSERT_POSITIONS:
            self.finale_insert_combo.addItem("", position)
        place_row.addWidget(self.finale_insert_combo)
        self.finale_prefix_check = QCheckBox()
        place_row.addWidget(self.finale_prefix_check)
        # 조합 생성 탭과 같은 설정이라 바뀌는 즉시 쓴다 (`commit()` 순서에 따라 옛 값이
        # 되써지지 않게).
        self.finale_insert_combo.currentIndexChanged.connect(self._on_finale_insert_changed)
        self.finale_prefix_check.toggled.connect(self._on_finale_prefix_toggled)
        place_row.addStretch(1)
        layout.addLayout(place_row)

        self.finale_button = QPushButton()
        self.finale_button.clicked.connect(self.start_finale)
        layout.addWidget(self.finale_button)
        self.finale_hint = QLabel()
        self.finale_hint.setWordWrap(True)
        layout.addWidget(self.finale_hint)

        self.finale_group = CollapsibleSection(i18n, "arena.finale_group")
        self.finale_group.set_content(body)
        return self.finale_group

    def _build_purge_section(self, i18n) -> CollapsibleSection:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.purge_matches_spin = QSpinBox()
        self.purge_matches_spin.setRange(1, 100)
        self.purge_matches_spin.valueChanged.connect(self._refresh_purge_preview)
        self.purge_matches_label = QLabel()
        form.addRow(self.purge_matches_label, self.purge_matches_spin)
        self.purge_elo_spin = QDoubleSpinBox()
        self.purge_elo_spin.setRange(0.0, 3000.0)
        self.purge_elo_spin.setSingleStep(10.0)
        self.purge_elo_spin.valueChanged.connect(self._refresh_purge_preview)
        self.purge_elo_label = QLabel()
        form.addRow(self.purge_elo_label, self.purge_elo_spin)
        layout.addLayout(form)

        purge_row = QHBoxLayout()
        self.purge_preview_label = QLabel()
        purge_row.addWidget(self.purge_preview_label, 1)
        self.purge_button = QPushButton()
        mark_danger(self.purge_button)
        self.purge_button.clicked.connect(self.purge)
        purge_row.addWidget(self.purge_button)
        layout.addLayout(purge_row)

        self.purge_artist_label = QLabel()
        layout.addWidget(self.purge_artist_label)
        artist_row = QHBoxLayout()
        self.artist_combo = QComboBox()
        artist_row.addWidget(self.artist_combo, 1)
        self.purge_artist_button = QPushButton()
        mark_danger(self.purge_artist_button)
        self.purge_artist_button.clicked.connect(self.purge_by_artist)
        artist_row.addWidget(self.purge_artist_button)
        layout.addLayout(artist_row)

        self.purge_group = CollapsibleSection(i18n, "arena.purge_group")
        self.purge_group.set_content(body)
        return self.purge_group

    def _build_archive_section(self, i18n) -> CollapsibleSection:
        """기록 파일과 초기화 — 되돌릴 수 없는 것들을 한자리에 모은다."""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        archive_row = QHBoxLayout()
        self.export_button = QPushButton()
        self.export_button.clicked.connect(self.export_archive)
        archive_row.addWidget(self.export_button)
        self.import_button = QPushButton()
        self.import_button.clicked.connect(self.import_archive)
        archive_row.addWidget(self.import_button)
        layout.addLayout(archive_row)

        self.cleanup_button = QPushButton()
        self.cleanup_button.clicked.connect(self.cleanup_orphans)
        layout.addWidget(self.cleanup_button)

        reset_row = QHBoxLayout()
        self.reset_matches_button = QPushButton()
        mark_danger(self.reset_matches_button)
        self.reset_matches_button.clicked.connect(self.reset_matches)
        reset_row.addWidget(self.reset_matches_button)
        self.reset_stats_button = QPushButton()
        mark_danger(self.reset_stats_button)
        self.reset_stats_button.clicked.connect(self.reset_stats)
        reset_row.addWidget(self.reset_stats_button)
        layout.addLayout(reset_row)

        self.archive_group = CollapsibleSection(i18n, "arena.archive_group")
        self.archive_group.set_content(body)
        return self.archive_group

    def _build_artists_section(self, i18n) -> CollapsibleSection:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.artists_label = QLabel()
        layout.addWidget(self.artists_label)
        self.artists_table = QTableWidget(0, 4)
        self.artists_table.verticalHeader().setVisible(False)
        self.artists_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.artists_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.artists_table)

        self.artists_group = CollapsibleSection(i18n, "arena.artists_group")
        self.artists_group.set_content(body)
        return self.artists_group

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._refresh_tiers()
        self._refresh_sheet()
        self._refresh_artists()
        self._refresh_generations()
        self._refresh_finale_hint()
        self._refresh_parents()
        self._refresh_artist_filter()
        self._refresh_purge_preview()
        self._refresh_detail()

    def commit(self) -> None:
        arena = self.arena
        arena.finale_top_n = self.finale_top_spin.value()
        arena.finale_per_combo = self.finale_per_spin.value()
        arena.finale_include_unrated = self.finale_unrated_check.isChecked()
        arena.finale_by_judge = self.finale_judge_check.isChecked()
        # insert_position·use_prefix는 바뀌는 즉시 저장한다 (`_on_finale_insert_changed`).
        arena.children_per_run = self.children_spin.value()
        arena.parent_pool = self.pool_spin.value()
        arena.mutation_rate = self.mutation_spin.value()
        arena.weight_jitter = self.jitter_spin.value()
        arena.purge_min_matches = self.purge_matches_spin.value()
        arena.purge_elo = self.purge_elo_spin.value()

    def retranslate(self) -> None:
        tr = self.tr
        self.sheet.setHorizontalHeaderLabels(
            [
                tr("arena.col_tier"),
                tr("arena.col_generation"),
                tr("arena.col_elo"),
                tr("arena.col_judge"),
                tr("arena.col_record"),
                tr("arena.col_artists"),
            ]
        )
        self.sort_label.setText(tr("arena.sort_sheet_by"))
        self.sort_combo.setItemText(0, tr("arena.sort_by_elo"))
        self.sort_combo.setItemText(1, tr("arena.sort_by_judge"))
        self.artists_table.setHorizontalHeaderLabels(
            [tr("arena.col_artist"), tr("arena.col_elo"), tr("arena.col_winrate"), tr("arena.col_uses")]
        )
        self.send_button.setText(tr("arena.send_to_prompt"))
        self.copy_button.setText(tr("arena.card_copy"))
        self.zoom_in_button.setToolTip(tr("arena.zoom_in"))
        self.zoom_out_button.setToolTip(tr("arena.zoom_out"))
        self.zoom_fit_button.setText(tr("arena.zoom_fit"))
        self.zoom_fit_button.setToolTip(tr("arena.zoom_fit_tip"))
        self.reset_matches_button.setText(tr("arena.reset_matches"))
        self.cleanup_button.setText(tr("arena.cleanup_orphans"))
        self.reset_stats_button.setText(tr("arena.reset_stats"))
        self.reset_matches_button.setToolTip(tr("arena.reset_matches_hint"))
        self.reset_stats_button.setToolTip(tr("arena.reset_stats_hint"))
        self.cleanup_button.setToolTip(tr("arena.cleanup_orphans_hint"))
        self.export_button.setText(tr("arena.export_archive"))
        self.export_button.setToolTip(tr("arena.export_archive_hint"))
        self.import_button.setText(tr("arena.import_archive"))
        self.import_button.setToolTip(tr("arena.import_archive_hint"))
        self.finale_top_label.setText(tr("arena.finale_top"))
        self.finale_per_label.setText(tr("arena.finale_per_combo"))
        self.finale_unrated_check.setText(tr("arena.finale_include_unrated"))
        self.finale_judge_check.setText(tr("arena.finale_by_judge"))
        self.finale_judge_check.setToolTip(tr("arena.finale_by_judge_hint"))
        self.finale_insert_label.setText(tr("arena.insert_position"))
        for index in range(self.finale_insert_combo.count()):
            key = self.finale_insert_combo.itemData(index)
            self.finale_insert_combo.setItemText(index, tr(f"arena.insert_{key}"))
        self.finale_prefix_check.setText(tr("arena.use_prefix"))
        self.finale_button.setText(tr("arena.finale_start"))
        self.finale_button.setToolTip(tr("arena.finale_start_hint"))
        self.children_label.setText(tr("arena.children_per_run"))
        self.pool_label.setText(tr("arena.parent_pool"))
        self.mutation_label.setText(tr("arena.mutation_rate"))
        self.jitter_label.setText(tr("arena.weight_jitter"))
        self.mutation_label.setToolTip(tr("arena.mutation_rate_hint"))
        self.jitter_label.setToolTip(tr("arena.weight_jitter_hint"))
        self.breed_button.setText(tr("arena.breed"))
        self.purge_matches_label.setText(tr("arena.purge_min_matches"))
        self.purge_elo_label.setText(tr("arena.purge_elo"))
        self.purge_button.setText(tr("arena.purge"))
        self.purge_artist_label.setText(tr("arena.purge_by_artist"))
        self.purge_artist_button.setText(tr("arena.purge_artist_button"))
        for section in (
            self.breed_group,
            self.finale_group,
            self.purge_group,
            self.archive_group,
            self.artists_group,
        ):
            section.retranslate()
        self.refresh()

    # ── 분포 ────────────────────────────────────────────────────────────

    def _refresh_tiers(self) -> None:
        counts = tier_counts([combo for combo in self.state.combos if combo.matches > 0])
        parts = [f"{tier} {counts[tier]}" for tier in TIERS]
        self.tier_label.setText(self.tr("arena.tier_sheet").format(" · ".join(parts)))
        self.tier_bar.set_counts([counts[tier] for tier in TIERS], list(TIERS))

    def _refresh_generations(self) -> None:
        counts: dict[int, int] = {}
        for combo in self.state.combos:
            counts[combo.generation] = counts.get(combo.generation, 0) + 1
        if not counts:
            self.generations_label.setText(self.tr("arena.generations_empty"))
            self.generation_bar.set_counts([])
            return
        order = sorted(counts)
        parts = [f"Gen.{gen} {counts[gen]}" for gen in order]
        self.generations_label.setText(self.tr("arena.generations").format(" · ".join(parts)))
        self.generation_bar.set_counts([counts[gen] for gen in order], [f"Gen.{gen}" for gen in order])

    # ── 순위표 ──────────────────────────────────────────────────────────

    def _refresh_sheet(self) -> None:
        combos = self._sorted_combos()
        self.sheet.setRowCount(len(combos))
        for row, combo in enumerate(combos):
            self._fill_sheet_row(row, combo)

    def _sorted_combos(self) -> list[Combo]:
        """지금 정렬 기준으로 줄 세운 조합 목록.

        `LLM 점수순`이면 판독 점수가 있는 조합만 점수순으로 올린다 (점수 없는 것은
        LLM 결산 대상이 아니므로 표에서도 빼, 순위 오해를 막는다). `Elo순`은 기존대로
        전부 올린다 — 사람 순위는 판독 여부와 무관하기 때문이다.
        """
        if self.sort_combo.currentData() == SORT_JUDGE:
            return judge_leaderboard_combos(self.state.combos)
        return leaderboard(self.state.combos)

    def _fill_sheet_row(self, row: int, combo: Combo) -> None:
        tr = self.tr
        mark = "" if is_settled(combo.matches) else tr("arena.tier_provisional_mark")
        judge_text = str(combo.judge_score) if combo.has_judge_score else "-"
        cells = (
            (COL_TIER, f"{tier_of(combo.elo)}{mark}" if combo.matches else "-"),
            (COL_GENERATION, str(combo.generation)),
            (COL_ELO, f"{combo.elo:.0f}" if combo.matches else "-"),
            (COL_JUDGE, judge_text),
            (COL_RECORD, f"{combo.wins}/{combo.matches}"),
            (COL_ARTISTS, format_artist_block(combo.slots, self.arena.use_prefix)),
        )
        for col, text in cells:
            item = QTableWidgetItem(text)
            if col == COL_TIER:
                # 어느 줄이 어느 조합인지 표를 다시 그려도 잃지 않게.
                item.setData(Qt.ItemDataRole.UserRole, combo.id)
            self.sheet.setItem(row, col, item)

    def _refresh_artists(self) -> None:
        entries = artist_ranking(self.state, ARTIST_TOP_N)
        self.artists_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            cells = (
                entry.name,
                f"{entry.elo:.0f}" if entry.matches else "-",
                f"{entry.win_rate * 100:.0f}% ({entry.wins}/{entry.matches})" if entry.matches else "-",
                str(entry.uses),
            )
            for col, text in enumerate(cells):
                self.artists_table.setItem(row, col, QTableWidgetItem(text))
        self.artists_label.setText(self.tr("arena.artist_ranking").format(len(entries)))

    # ── 고른 조합 ───────────────────────────────────────────────────────

    def selected_combo(self) -> Combo | None:
        return self.selected_combo_in(self.sheet, COL_TIER)

    def _refresh_detail(self) -> None:
        """고른 줄의 그림과 프롬프트. 통계 탭에서는 태그 문자열만 보였다."""
        tr = self.tr
        combo = self.selected_combo()
        enabled = combo is not None
        self.send_button.setEnabled(enabled)
        self.copy_button.setEnabled(enabled)
        if combo is None:
            self.detail_image.setText(tr("arena.result_no_selection"))
            self.detail_label.setText("")
            self.detail_prompt.setText("")
            self._refresh_zoom_controls()
            return

        self.detail_label.setText(self._detail_text(combo))
        self.detail_prompt.setText(format_artist_block(combo.slots, self.arena.use_prefix))
        pixmap = self._pixmap_for(combo)
        if pixmap is None:
            self.detail_image.setText(tr("arena.card_no_image"))
            self._refresh_zoom_controls()
            return
        # 새 그림은 맞춘 크기에서 시작한다 — 비율을 지켜 넣으므로 위아래가 잘리지 않는다.
        self.detail_image.setPixmap(pixmap)
        self._refresh_zoom_controls()

    def _detail_text(self, combo: Combo) -> str:
        tr = self.tr
        tier = tier_of(combo.elo) if combo.matches else "-"
        colour = tier_color(self.palette(), tier)
        shown = tier if colour is None else f'<span style="color:{colour}">{tier}</span>'
        judge = str(combo.judge_score) if combo.has_judge_score else "-"
        return tr("arena.result_detail").format(
            combo.generation, shown, round(combo.elo), judge, combo.wins, combo.matches
        )

    def _pixmap_for(self, combo: Combo) -> QPixmap | None:
        return combo_pixmap(self._service.store, combo)

    def _selected_block(self) -> str:
        combo = self.selected_combo()
        return format_artist_block(combo.slots, self.arena.use_prefix) if combo else ""

    def send_selected_to_prompt(self) -> bool:
        block = self._selected_block()
        if not block:
            self.status_message.emit(self.tr("arena.select_a_row"))
            return False
        self.prompt_selected.emit(block)
        return True

    def copy_selected(self) -> bool:
        if not self.copy_block_to_clipboard(self.selected_combo()):
            self.status_message.emit(self.tr("arena.select_a_row"))
            return False
        self.status_message.emit(self.tr("arena.copied"))
        return True

    # ── 교배 ────────────────────────────────────────────────────────────

    def parent_candidates(self) -> list[Combo]:
        return select_parents(self.state.combos, self.pool_spin.value())

    def _refresh_parents(self) -> None:
        parents = self.parent_candidates()
        self.parents_list.clear()
        for combo in parents:
            item = QListWidgetItem(self._parent_text(combo))
            item.setData(Qt.ItemDataRole.UserRole, combo.id)
            icon = self._thumbnail(combo)
            if icon is not None:
                item.setIcon(icon)
            self.parents_list.addItem(item)
        self.parents_label.setText(self.tr("arena.parents").format(len(parents)))
        enough = len(parents) >= 2
        self.breed_button.setEnabled(enough)
        # 비활성인 채로 침묵하지 않는다 — 무엇이 모자란지 말한다.
        self.breed_hint.setText("" if enough else self.tr("arena.breed_need_parents"))
        self.breed_hint.setVisible(not enough)

    def _parent_text(self, combo: Combo) -> str:
        block = format_artist_block(combo.slots, self.arena.use_prefix)
        return self.tr("arena.parent_row").format(
            combo.generation, tier_of(combo.elo), round(combo.elo), combo.wins, combo.matches, block
        )

    def _thumbnail(self, combo: Combo) -> QIcon | None:
        """부모 후보의 그림 — 태그 문자열만 보고 고르라는 것은 불친절하다."""
        return combo_icon(self._service.store, combo)

    def breed_children(self) -> int:
        """상위 조합을 교배해 다음 세대를 만든다. 만든 수를 돌려준다."""
        self.commit()
        children = breed(
            self.state,
            self.arena.combo_params(),
            self.arena.children_per_run,
            self.arena.parent_pool,
        )
        if not children:
            self.status_message.emit(self.tr("arena.breed_need_parents"))
            return 0
        self.state.combos.extend(children)
        self._service.save()
        generation = max(child.generation for child in children)
        self.status_message.emit(self.tr("arena.bred").format(len(children), generation))
        self.state_changed.emit()
        return len(children)

    # ── 정리 ────────────────────────────────────────────────────────────

    def _refresh_artist_filter(self) -> None:
        """조합에 실제로 들어 있는 작가만 올린다 — 지울 대상이 없는 이름은 소용없다."""
        current = self.artist_combo.currentText()
        names = sorted({slot.name for combo in self.state.combos for slot in combo.slots})
        self.artist_combo.clear()
        self.artist_combo.addItems(names)
        index = self.artist_combo.findText(current)
        if index >= 0:
            self.artist_combo.setCurrentIndex(index)
        self.purge_artist_button.setEnabled(bool(names))

    def purge_targets(self) -> list[Combo]:
        return purge_candidates(
            self.state.combos, self.purge_matches_spin.value(), self.purge_elo_spin.value()
        )

    def _refresh_purge_preview(self) -> None:
        count = len(self.purge_targets())
        self.purge_preview_label.setText(self.tr("arena.purge_preview").format(count))
        self.purge_button.setEnabled(count > 0)

    def purge(self) -> int:
        """조건에 맞는 조합을 지운다 (확인 후). 지운 수를 돌려준다."""
        targets = self.purge_targets()
        if not targets or not self._confirm(len(targets)):
            return 0
        return self._remove(targets)

    def purge_by_artist(self) -> int:
        """고른 작가가 낀 조합을 한 번에 지운다 (확인 후).

        "이 작가는 내 취향이 아니다"를 알게 됐을 때 관련 조합을 치우는 용도다.
        """
        name = self.artist_combo.currentText()
        if not name:
            return 0
        targets = combos_with_artist(self.state.combos, name)
        if not targets:
            self.status_message.emit(self.tr("arena.purge_none"))
            return 0
        if not self._confirm(len(targets)):
            return 0
        return self._remove(targets)

    def _confirm(self, count: int) -> bool:
        """한 번에 여러 개가 사라지므로 반드시 묻는다.

        문구는 `그림 파일도 함께 삭제` 설정에 따라 다르다 — 파일을 남기는 기본
        설정에서는 `되돌리기`로 온전히 되살릴 수 있다는 것까지 알려 준다.
        """
        key = "arena.purge_confirm" if self.arena.delete_images_with_combo else "arena.purge_confirm_keep"
        answer = QMessageBox.question(self, self.tr("arena.purge"), self.tr(key).format(count))
        return answer == QMessageBox.StandardButton.Yes

    def _remove(self, targets: list[Combo]) -> int:
        """서비스를 거쳐 지운다 — 잘못 눌러도 `되돌리기`로 되살릴 수 있다."""
        removed = self._service.remove_combos(targets, delete_images=self.arena.delete_images_with_combo)
        self.status_message.emit(self.tr("arena.deleted_undoable").format(removed))
        self.state_changed.emit()
        return removed

    # ── 초기화 ──────────────────────────────────────────────────────────

    def reset_matches(self) -> bool:
        """대전 기록만 지운다 — 작가 명단과 조합, 그림은 그대로 남는다."""
        if not self.state.total_matches and not any(c.matches for c in self.state.combos):
            self.status_message.emit(self.tr("arena.nothing_to_reset"))
            return False
        answer = QMessageBox.question(
            self,
            self.tr("arena.reset_matches"),
            self.tr("arena.reset_matches_confirm").format(self.state.total_matches),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.state.reset_matches()
        self._service.save()
        self.status_message.emit(self.tr("arena.matches_reset"))
        self.state_changed.emit()
        return True

    def reset_stats(self) -> int:
        """통계를 통째로 비운다 — 조합·점수·전적·세대까지. 지운 조합 수를 돌려준다.

        `대전 기록 초기화`는 점수만 0으로 돌리고 조합은 남긴다. 그래서 세대 분포와
        티어 시트에 줄이 그대로 남아 "초기화가 안 된다"는 제보가 나왔다. 여기는
        조합 자체를 지워 표를 비운다.

        **작가 명단은 남긴다** — 애써 모은 후보까지 날아가면 곤란하다는 것이 둘을
        나눈 이유다. 명단을 비우려면 작가 명단 탭의 `전체 삭제`를 쓴다.
        """
        combos = list(self.state.combos)
        if not combos and not self.state.total_matches:
            self.status_message.emit(self.tr("arena.nothing_to_reset"))
            return 0
        with_image = sum(1 for combo in combos if combo.has_image)
        answer = QMessageBox.question(
            self,
            self.tr("arena.reset_stats"),
            self.tr("arena.reset_stats_confirm").format(len(combos), self.state.total_matches, with_image),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 0
        # 서비스를 거친다 — 잘못 눌러도 조합 생성 탭의 `되돌리기`로 되살아난다.
        removed = self._service.remove_combos(combos, delete_images=self.arena.delete_images_with_combo)
        self.state.reset_matches()  # 남은 작가 점수까지 0으로
        self._service.save()
        self.status_message.emit(self.tr("arena.stats_reset").format(removed))
        self.state_changed.emit()
        return removed

    def cleanup_orphans(self) -> int:
        """어떤 조합도 쓰지 않는 그림 파일을 지운다.

        중간에 앱이 죽거나 `arena.json`을 손댔을 때 남는다.
        """
        removed = self._service.store.cleanup_orphans(self.state)
        self.status_message.emit(self.tr("arena.orphans_cleaned").format(removed))
        return removed

    # ── 결산 ────────────────────────────────────────────────────────────

    def finale_selection(self) -> list[Combo]:
        """지금 설정으로 결산에 올라갈 조합 — 표에 보이는 순서 그대로 위에서부터.

        `LLM 점수 순` 체크가 켜져 있으면 사람 Elo 대신 LLM 판독 점수로 뽑는다.
        """
        return finale_combos(
            self.state,
            self.finale_top_spin.value(),
            include_unrated=self.finale_unrated_check.isChecked(),
            by_judge=self.finale_judge_check.isChecked(),
        )

    def _on_finale_judge_toggled(self, _checked: bool) -> None:
        self._sync_finale_checks()
        self._refresh_finale_hint()

    def _sync_finale_checks(self) -> None:
        """LLM 점수 순이면 `전적 없는 조합도`는 뜻이 없다 — LLM 결산은 점수가 있는 것만
        올리기 때문이다. 오해를 막으려 비활성화한다."""
        by_judge = self.finale_judge_check.isChecked()
        self.finale_unrated_check.setEnabled(not by_judge)

    def start_finale(self) -> int:
        """결산 생성을 요청한다. 실제로 돌릴 장수를 돌려준다 (못 돌리면 0).

        생성은 메인 창이 자기 파이프라인으로 한다 — 그래야 결과가 **결과 폴더**에
        쌓이고 갤러리에도 뜬다.
        """
        combos = self.finale_selection()
        if not combos:
            self.status_message.emit(self.tr("arena.finale_nothing"))
            return 0
        per_combo = self.finale_per_spin.value()
        total = finale_total(combos, per_combo)
        answer = QMessageBox.question(
            self,
            self.tr("arena.finale_start"),
            self.tr("arena.finale_confirm").format(len(combos), per_combo, total),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 0
        self.commit()
        self.finale_requested.emit(combos, per_combo)
        return total

    def _refresh_finale_hint(self) -> None:
        """몇 장이 나가는지 늘 보이게 — 크레딧을 쓰는 버튼이다."""
        combos = self.finale_selection()
        total = finale_total(combos, self.finale_per_spin.value())
        self.finale_hint.setText(self.tr("arena.finale_hint").format(len(combos), total))
        self.finale_button.setEnabled(bool(combos))

    # ── 기록 파일 ───────────────────────────────────────────────────────

    def export_archive(self) -> str:
        """기록과 그림을 zip 하나로 담는다. 저장한 경로를 돌려준다 (취소하면 "")."""
        if not self.state.combos:
            self.status_message.emit(self.tr("arena.nothing_to_export"))
            return ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("arena.export_archive"),
            str(Path(self._service.store.base_dir) / self._suggested_name()),
            self.tr("arena.archive_filter"),
        )
        if not path:
            return ""
        try:
            summary = self._service.export_archive(Path(path), app_version=__version__)
        except ArchiveError as e:
            logger.error("arena export failed: %s", e)
            QMessageBox.warning(self, self.tr("arena.export_archive"), self.tr("arena.export_failed"))
            return ""
        self.status_message.emit(
            self.tr("arena.exported").format(summary.combos, summary.images, Path(path).name)
        )
        return path

    def import_archive(self) -> str:
        """기록 파일을 읽어 지금 기록을 갈아 끼운다. 읽은 경로를 돌려준다.

        지금 것이 사라지므로, **무엇이 사라지고 무엇이 들어오는지** 둘 다 보여 주고
        확인을 받는다.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("arena.import_archive"),
            str(self._service.store.base_dir),
            self.tr("arena.archive_filter"),
        )
        if not path:
            return ""
        try:
            incoming = read_summary(Path(path))
        except ArchiveError as e:
            logger.error("arena archive unreadable (%s): %s", path, e)
            QMessageBox.warning(self, self.tr("arena.import_archive"), self.tr("arena.archive_unreadable"))
            return ""
        if not self._confirm_import(incoming):
            return ""
        try:
            summary = self._service.import_archive(Path(path))
        except ArchiveError as e:
            logger.error("arena import failed: %s", e)
            QMessageBox.warning(self, self.tr("arena.import_archive"), self.tr("arena.import_failed"))
            return ""
        self.status_message.emit(self.tr("arena.imported").format(summary.combos, summary.images))
        self.state_changed.emit()
        return path

    def _confirm_import(self, incoming: ArchiveSummary) -> bool:
        answer = QMessageBox.question(
            self,
            self.tr("arena.import_archive"),
            self.tr("arena.import_confirm").format(
                incoming.combos,
                incoming.images,
                incoming.matches,
                len(self.state.combos),
                self.state.total_matches,
            ),
        )
        return answer == QMessageBox.StandardButton.Yes

    def _suggested_name(self) -> str:
        """`arena-20260908-1430.zip` — 여러 벌을 모아 둬도 언제 것인지 보이게."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return f"arena-{stamp}{ARCHIVE_SUFFIX}"

    # ── 설정 ────────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        # 조합 생성 탭에서 바꾼 위치·접두사를 다시 보여 준다 (같은 설정을 공유한다).
        self._load_placement()
        super().showEvent(event)

    def _on_finale_insert_changed(self) -> None:
        self.arena.insert_position = self.finale_insert_combo.currentData()

    def _on_finale_prefix_toggled(self, checked: bool) -> None:
        self.arena.use_prefix = checked

    def _load_placement(self) -> None:
        index = self.finale_insert_combo.findData(self.arena.insert_position)
        if index >= 0:
            self.finale_insert_combo.setCurrentIndex(index)
        self.finale_prefix_check.setChecked(self.arena.use_prefix)

    def _load_settings(self) -> None:
        arena = self.arena
        self.finale_top_spin.setValue(max(1, min(MAX_TOP_N, arena.finale_top_n)))
        self.finale_per_spin.setValue(max(1, min(MAX_PER_COMBO, arena.finale_per_combo)))
        self.finale_unrated_check.setChecked(arena.finale_include_unrated)
        self.finale_judge_check.setChecked(arena.finale_by_judge)
        self._load_placement()
        self._sync_finale_checks()
        self.children_spin.setValue(arena.children_per_run)
        self.pool_spin.setValue(arena.parent_pool)
        self.mutation_spin.setValue(arena.mutation_rate)
        self.jitter_spin.setValue(arena.weight_jitter)
        self.purge_matches_spin.setValue(arena.purge_min_matches)
        self.purge_elo_spin.setValue(arena.purge_elo)


__all__ = [
    "ARTIST_TOP_N",
    "COL_ARTISTS",
    "COL_ELO",
    "COL_TIER",
    "PREVIEW_HEIGHT",
    "THUMBNAIL_SIZE",
    "ResultTab",
]
