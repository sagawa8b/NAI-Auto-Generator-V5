"""그림체 개선 탭 — 상위 조합을 교배하고, 하위 조합을 정리한다.

월드컵으로 점수가 매겨진 조합을 부모로 삼아 자식을 낸다. 자식은 작가 구성뿐 아니라
**가중치도 물려받는다** — 원본은 매 세대 가중치를 새로 뽑아 "이 작가를 세게 쓴 게
좋았다"는 정보를 버렸다.

부모 후보를 **썸네일과 함께** 보여 준다. "그림체 개선 창에서 선택한 그림을 볼 수
있었으면 좋겠다"는 요청이 있었는데, 얼굴 없이 태그 문자열만 보고 교배할 부모를
고르라는 것은 확실히 불친절하다.

정리(물갈이)는 지우기 전에 **대상을 먼저 보여 준다.** 삭제는 되돌릴 수 없고, 그림
파일까지 함께 사라진다.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import format_artist_block
from ...core.arena.elo import tier_of
from ...core.arena.evolution import (
    breed,
    combos_with_artist,
    purge_candidates,
    select_parents,
)
from ...core.arena.models import Combo
from .base import ArenaTab

logger = logging.getLogger(__name__)

#: 부모 목록 썸네일 크기 (논리 픽셀).
THUMBNAIL_SIZE = 72


class EvolveTab(ArenaTab):
    """교배와 정리."""

    KEY = "evolve"

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)

        layout = QVBoxLayout(self)

        # ── 부모 후보 ───────────────────────────────────────────────────
        self.parents_label = QLabel()
        layout.addWidget(self.parents_label)
        self.parents_list = QListWidget()
        self.parents_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        layout.addWidget(self.parents_list, 1)

        # ── 교배 ────────────────────────────────────────────────────────
        self.breed_group = QGroupBox()
        breed_form = QFormLayout(self.breed_group)
        self.children_spin = QSpinBox()
        self.children_spin.setRange(1, 100)
        self.children_label = QLabel()
        breed_form.addRow(self.children_label, self.children_spin)
        self.pool_spin = QSpinBox()
        self.pool_spin.setRange(2, 100)
        self.pool_label = QLabel()
        breed_form.addRow(self.pool_label, self.pool_spin)
        self.mutation_spin = QDoubleSpinBox()
        self.mutation_spin.setRange(0.0, 1.0)
        self.mutation_spin.setSingleStep(0.05)
        self.mutation_label = QLabel()
        breed_form.addRow(self.mutation_label, self.mutation_spin)
        self.jitter_spin = QDoubleSpinBox()
        self.jitter_spin.setRange(0.0, 1.0)
        self.jitter_spin.setSingleStep(0.05)
        self.jitter_label = QLabel()
        breed_form.addRow(self.jitter_label, self.jitter_spin)
        self.breed_button = QPushButton()
        self.breed_button.clicked.connect(self.breed_children)
        breed_form.addRow(self.breed_button)
        layout.addWidget(self.breed_group)

        # ── 정리 ────────────────────────────────────────────────────────
        self.purge_group = QGroupBox()
        purge_form = QFormLayout(self.purge_group)
        self.purge_matches_spin = QSpinBox()
        self.purge_matches_spin.setRange(1, 100)
        self.purge_matches_spin.valueChanged.connect(self._refresh_purge_preview)
        self.purge_matches_label = QLabel()
        purge_form.addRow(self.purge_matches_label, self.purge_matches_spin)
        self.purge_elo_spin = QDoubleSpinBox()
        self.purge_elo_spin.setRange(0.0, 3000.0)
        self.purge_elo_spin.setSingleStep(10.0)
        self.purge_elo_spin.valueChanged.connect(self._refresh_purge_preview)
        self.purge_elo_label = QLabel()
        purge_form.addRow(self.purge_elo_label, self.purge_elo_spin)
        purge_row = QHBoxLayout()
        self.purge_preview_label = QLabel()
        purge_row.addWidget(self.purge_preview_label, 1)
        self.purge_button = QPushButton()
        self.purge_button.clicked.connect(self.purge)
        purge_row.addWidget(self.purge_button)
        purge_form.addRow(purge_row)

        artist_row = QHBoxLayout()
        self.artist_combo = QComboBox()
        artist_row.addWidget(self.artist_combo, 1)
        self.purge_artist_button = QPushButton()
        self.purge_artist_button.clicked.connect(self.purge_by_artist)
        artist_row.addWidget(self.purge_artist_button)
        self.purge_artist_label = QLabel()
        purge_form.addRow(self.purge_artist_label, artist_row)
        layout.addWidget(self.purge_group)

        self._load_settings()
        self.retranslate()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        self._refresh_parents()
        self._refresh_artists()
        self._refresh_purge_preview()

    def commit(self) -> None:
        arena = self.arena
        arena.children_per_run = self.children_spin.value()
        arena.parent_pool = self.pool_spin.value()
        arena.mutation_rate = self.mutation_spin.value()
        arena.weight_jitter = self.jitter_spin.value()
        arena.purge_min_matches = self.purge_matches_spin.value()
        arena.purge_elo = self.purge_elo_spin.value()

    def retranslate(self) -> None:
        tr = self.tr
        self.breed_group.setTitle(tr("arena.breed_group"))
        self.purge_group.setTitle(tr("arena.purge_group"))
        self.children_label.setText(tr("arena.children_per_run"))
        self.pool_label.setText(tr("arena.parent_pool"))
        self.mutation_label.setText(tr("arena.mutation_rate"))
        self.jitter_label.setText(tr("arena.weight_jitter"))
        self.breed_button.setText(tr("arena.breed"))
        self.purge_matches_label.setText(tr("arena.purge_min_matches"))
        self.purge_elo_label.setText(tr("arena.purge_elo"))
        self.purge_button.setText(tr("arena.purge"))
        self.purge_artist_label.setText(tr("arena.purge_by_artist"))
        self.purge_artist_button.setText(tr("arena.purge_artist_button"))
        self.mutation_label.setToolTip(tr("arena.mutation_rate_hint"))
        self.jitter_label.setToolTip(tr("arena.weight_jitter_hint"))
        self.refresh()

    # ── 부모 목록 ───────────────────────────────────────────────────────

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
        self.breed_button.setEnabled(len(parents) >= 2)

    def _parent_text(self, combo: Combo) -> str:
        block = format_artist_block(combo.slots, self.arena.use_prefix)
        return self.tr("arena.parent_row").format(
            combo.generation, tier_of(combo.elo), round(combo.elo), combo.wins, combo.matches, block
        )

    def _thumbnail(self, combo: Combo) -> QIcon | None:
        """부모 후보의 그림 — 태그 문자열만 보고 고르라는 것은 불친절하다."""
        path = self._service.store.image_path(combo)
        if path is None:
            return None
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return None
        return QIcon(pixmap)

    def _refresh_artists(self) -> None:
        """조합에 실제로 들어 있는 작가만 올린다 — 지울 대상이 없는 이름은 소용없다."""
        current = self.artist_combo.currentText()
        names = sorted({slot.name for combo in self.state.combos for slot in combo.slots})
        self.artist_combo.clear()
        self.artist_combo.addItems(names)
        index = self.artist_combo.findText(current)
        if index >= 0:
            self.artist_combo.setCurrentIndex(index)
        self.purge_artist_button.setEnabled(bool(names))

    # ── 교배 ────────────────────────────────────────────────────────────

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

    # ── 설정 ────────────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        arena = self.arena
        self.children_spin.setValue(arena.children_per_run)
        self.pool_spin.setValue(arena.parent_pool)
        self.mutation_spin.setValue(arena.mutation_rate)
        self.jitter_spin.setValue(arena.weight_jitter)
        self.purge_matches_spin.setValue(arena.purge_min_matches)
        self.purge_elo_spin.setValue(arena.purge_elo)


__all__ = ["THUMBNAIL_SIZE", "EvolveTab"]
