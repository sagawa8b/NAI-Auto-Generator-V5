"""통계 탭 — 지금까지 알아낸 것을 한 화면에 모은다.

세 가지를 본다.

- **티어 시트**: 조합을 S~F로 줄 세운 표. 여기서 마음에 든 조합을 골라 메인 창
  프롬프트로 보내거나 클립보드로 복사한다 — 아레나를 돌리는 목적이 결국 이것이다.
- **작가 순위**: 여러 조합에 걸쳐 꾸준히 이긴 작가. 조합이 아니라 **작가** 단위로
  취향을 알려 준다.
- **세대 분포**: 교배가 몇 세대까지 갔는지.

초기화는 둘로 나눠 뒀다. "작가 등록 쪽 전체 초기화를 눌렀더니 작가 목록까지
초기 버전으로 돌아갔다"는 제보가 있었는데, 대전 기록만 지우고 싶은 경우가 훨씬 많다.

**기록 내보내기/불러오기**도 여기 있다. 아레나 폴더는 하나뿐이라, 다른 주제로 새로
시작하려면 지금까지 쌓은 것을 덮어써야 했다. 기록과 그림을 zip 하나에 담아 두면
(`core/arena/archive.py`) 나중에 그대로 되살릴 수 있다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
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
from ...core.arena.finale import (
    MAX_PER_COMBO,
    MAX_TOP_N,
    finale_combos,
    finale_total,
    judge_leaderboard_combos,
)
from ...core.arena.models import Combo
from .base import ArenaTab

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


class StatsTab(ArenaTab):
    """티어 시트 · 작가 순위 · 세대 분포."""

    KEY = "stats"

    #: 고른 조합을 메인 창 프롬프트로 (다이얼로그가 밖으로 넘긴다).
    prompt_selected = Signal(str)

    #: 결산 생성 요청 — (조합 목록, 조합당 장수). 메인 창이 자기 생성 경로로 돌린다.
    finale_requested = Signal(list, int)

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)

        layout = QVBoxLayout(self)

        self.tier_label = QLabel()
        self.tier_label.setWordWrap(True)
        layout.addWidget(self.tier_label)

        # 정렬 기준 — 사람 Elo 순(기본)과 LLM 판독 점수 순. 두 점수를 나란히
        # 보여 주되, 어느 쪽으로 줄 세울지 고르게 한다.
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
        header = self.sheet.horizontalHeader()
        header.setSectionResizeMode(COL_ARTISTS, QHeaderView.ResizeMode.Stretch)
        for col in (COL_TIER, COL_GENERATION, COL_ELO, COL_JUDGE, COL_RECORD):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.sheet, 2)

        actions = QHBoxLayout()
        self.send_button = QPushButton()
        self.send_button.clicked.connect(self.send_selected_to_prompt)
        actions.addWidget(self.send_button)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self.copy_selected)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        self.reset_matches_button = QPushButton()
        self.reset_matches_button.clicked.connect(self.reset_matches)
        actions.addWidget(self.reset_matches_button)
        self.reset_stats_button = QPushButton()
        self.reset_stats_button.clicked.connect(self.reset_stats)
        actions.addWidget(self.reset_stats_button)
        self.cleanup_button = QPushButton()
        self.cleanup_button.clicked.connect(self.cleanup_orphans)
        actions.addWidget(self.cleanup_button)
        layout.addLayout(actions)

        # 결산 — 순위대로 실제 그림을 뽑는다. 여기만 크레딧을 쓴다.
        finale_row = QHBoxLayout()
        self.finale_top_label = QLabel()
        finale_row.addWidget(self.finale_top_label)
        self.finale_top_spin = QSpinBox()
        self.finale_top_spin.setRange(1, MAX_TOP_N)
        self.finale_top_spin.valueChanged.connect(self._refresh_finale_hint)
        finale_row.addWidget(self.finale_top_spin)
        self.finale_per_label = QLabel()
        finale_row.addWidget(self.finale_per_label)
        self.finale_per_spin = QSpinBox()
        self.finale_per_spin.setRange(1, MAX_PER_COMBO)
        self.finale_per_spin.valueChanged.connect(self._refresh_finale_hint)
        finale_row.addWidget(self.finale_per_spin)
        self.finale_unrated_check = QCheckBox()
        self.finale_unrated_check.toggled.connect(self._refresh_finale_hint)
        finale_row.addWidget(self.finale_unrated_check)
        self.finale_judge_check = QCheckBox()
        self.finale_judge_check.toggled.connect(self._on_finale_judge_toggled)
        finale_row.addWidget(self.finale_judge_check)
        self.finale_button = QPushButton()
        self.finale_button.clicked.connect(self.start_finale)
        finale_row.addWidget(self.finale_button)
        finale_row.addStretch(1)
        layout.addLayout(finale_row)

        self.finale_hint = QLabel()
        self.finale_hint.setWordWrap(True)
        layout.addWidget(self.finale_hint)

        # 기록 파일 — 지금 한 벌을 통째로 담아 두고, 나중에 그대로 되살린다.
        archive_actions = QHBoxLayout()
        self.export_button = QPushButton()
        self.export_button.clicked.connect(self.export_archive)
        archive_actions.addWidget(self.export_button)
        self.import_button = QPushButton()
        self.import_button.clicked.connect(self.import_archive)
        archive_actions.addWidget(self.import_button)
        archive_actions.addStretch(1)
        layout.addLayout(archive_actions)

        self.artists_label = QLabel()
        layout.addWidget(self.artists_label)
        self.artists_table = QTableWidget(0, 4)
        self.artists_table.verticalHeader().setVisible(False)
        self.artists_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.artists_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.artists_table, 1)

        self.generations_label = QLabel()
        self.generations_label.setWordWrap(True)
        layout.addWidget(self.generations_label)

        arena = self.arena
        self.finale_top_spin.setValue(max(1, min(MAX_TOP_N, arena.finale_top_n)))
        self.finale_per_spin.setValue(max(1, min(MAX_PER_COMBO, arena.finale_per_combo)))
        self.finale_unrated_check.setChecked(arena.finale_include_unrated)
        self.finale_judge_check.setChecked(arena.finale_by_judge)
        self._sync_finale_checks()

        self.retranslate()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._refresh_tiers()
        self._refresh_sheet()
        self._refresh_artists()
        self._refresh_generations()
        self._refresh_finale_hint()

    def commit(self) -> None:
        arena = self.arena
        arena.finale_top_n = self.finale_top_spin.value()
        arena.finale_per_combo = self.finale_per_spin.value()
        arena.finale_include_unrated = self.finale_unrated_check.isChecked()
        arena.finale_by_judge = self.finale_judge_check.isChecked()

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
        self.finale_button.setText(tr("arena.finale_start"))
        self.finale_button.setToolTip(tr("arena.finale_start_hint"))
        self.refresh()

    # ── 표 채우기 ───────────────────────────────────────────────────────

    def _refresh_tiers(self) -> None:
        counts = tier_counts([combo for combo in self.state.combos if combo.matches > 0])
        parts = [f"{tier} {counts[tier]}" for tier in TIERS]
        self.tier_label.setText(self.tr("arena.tier_sheet").format(" · ".join(parts)))

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

    def _refresh_generations(self) -> None:
        counts: dict[int, int] = {}
        for combo in self.state.combos:
            counts[combo.generation] = counts.get(combo.generation, 0) + 1
        if not counts:
            self.generations_label.setText(self.tr("arena.generations_empty"))
            return
        parts = [f"Gen.{gen} {counts[gen]}" for gen in sorted(counts)]
        self.generations_label.setText(self.tr("arena.generations").format(" · ".join(parts)))

    # ── 선택 ────────────────────────────────────────────────────────────

    def selected_combo(self) -> Combo | None:
        rows = {index.row() for index in self.sheet.selectedIndexes()}
        if not rows:
            return None
        item = self.sheet.item(min(rows), COL_TIER)
        return self.state.combo(str(item.data(Qt.ItemDataRole.UserRole))) if item else None

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
        block = self._selected_block()
        if not block:
            self.status_message.emit(self.tr("arena.select_a_row"))
            return False
        QGuiApplication.clipboard().setText(block)
        self.status_message.emit(self.tr("arena.copied"))
        return True

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


__all__ = ["ARTIST_TOP_N", "COL_ARTISTS", "COL_ELO", "COL_TIER", "StatsTab"]
