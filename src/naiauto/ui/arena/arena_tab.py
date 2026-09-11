"""이상형 월드컵 탭 — 두 그림을 보고 마음에 드는 쪽을 고른다.

한 번 고를 때마다 두 조합의 Elo가 갱신되고, 그 조합에 낀 작가들도 점수를 나눠 받는다.
대진은 무작위가 아니라 **덜 싸운 조합 우선 + 점수가 가까운 상대**로 짠다
(`core/arena/elo.pick_match`) — 같은 클릭 수로 순위가 더 정확해진다.

## 화면을 그림에 몰아준다

시간의 대부분을 여기서 쓴다. 그런데 예전에는 그림 위아래로 컨트롤 줄이 다섯 개
(리그 줄 · 진행 한 줄 · 선택 버튼 · 카드 조작 6개씩 · 판정 5개 · 안내 한 줄) 깔려
정작 볼 것이 밀려났다. 세 가지를 정리했다.

- **단축키를 버튼 라벨 안으로** — `← → 승자 · ↑ 둘 다 좋음 …` 안내문 한 줄이 통째로
  없어졌다. 누를 버튼 자신이 어떤 키인지 말한다 (단축키를 끄면 표기도 사라진다).
- **카드 조작 버튼은 그림 위로** — `combo_card.py` 참고. 되돌릴 수 없는 `삭제`가
  `복사` 옆에 똑같이 생긴 채로 상시 노출돼 있었다.
- **빈 화면은 막다른 길이 아니다** — 왜 겨룰 수 없는지(조합이 없다 / 그림이 없다 /
  리그가 좁다)를 구분해 말하고, 그것을 푸는 탭으로 가는 버튼을 함께 준다.

커뮤니티 요청을 반영한 것들:

- **둘 다 승 / 둘 다 패 / 스킵 / 양쪽 삭제** — 원본에는 스킵밖에 없었다.
- **방향키 단축키** — 손을 마우스로 옮기지 않고 계속 고를 수 있다. 탭 위젯의
  `keyPressEvent`만으로는 **거의 동작하지 않았다**: 키는 포커스를 가진 위젯으로
  가는데, 그림이나 단추를 한 번 누르는 순간 포커스는 거기로 옮겨 가 방향키는
  슬라이더가, `Space`는 단추가 먼저 먹었다. 그래서 이 탭에 붙인 `QShortcut`으로
  받는다 (`WidgetWithChildrenShortcut` — 이 탭 안에 포커스가 있을 때만 산다).
  `단축키 사용` 체크로 통째로 끌 수 있다.
- **이미지 배율** — 고해상도 모니터에서 그림이 작다는 지적.
- **미확정 개수 상시 표시** — "지금쯤 다 확정됐으려나?" 하고 다른 탭을 들락거리지
  않아도 되게. 전부 확정되면 그때 알린다.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import format_artist_block
from ...core.arena.elo import (
    RESULT_A,
    RESULT_B,
    RESULT_BOTH_LOSE,
    RESULT_BOTH_WIN,
    RESULT_DRAW,
    apply_match,
    is_settled,
    pick_match,
    unsettled,
)
from ...core.arena.evolution import regenerate_weights
from ...core.arena.guidance import MIN_COMBOS_TO_MATCH, STEP_GENERATE, STEP_MAKE_COMBOS
from ...core.arena.models import DEFAULT_ELO, Combo
from ...services.arena_service import ArenaImageReady, pending_combos, ready_combos
from ..widgets import EmptyState
from ..widgets.zoomable_image_view import ZoomableImageView
from .base import ArenaTab
from .combo_card import ComboCard
from .style import mark_danger
from .thumbnails import combo_pixmap

logger = logging.getLogger(__name__)

LEAGUE_ALL = "all"
LEAGUE_FAVORITES = "favorites"
LEAGUE_UNSETTLED = "unsettled"
LEAGUES = (LEAGUE_ALL, LEAGUE_FAVORITES, LEAGUE_UNSETTLED)

#: 빈 화면의 행동 중 탭 이동이 아닌 것 — 리그를 `전체`로 되돌린다.
_ACTION_ALL_LEAGUE = "all_league"

#: 행동 → 버튼에 쓸 i18n 키.
_ACTION_LABELS = {
    STEP_MAKE_COMBOS: "arena.empty_go_build",
    STEP_GENERATE: "arena.empty_go_generate",
    _ACTION_ALL_LEAGUE: "arena.empty_show_all",
}

#: 판정 버튼에 붙일 단축키 표시. `비김`은 키를 배정하지 않았다.
_JUDGE_KEYS = {
    RESULT_BOTH_WIN: "↑",
    RESULT_BOTH_LOSE: "↓",
}

#: 배율 슬라이더 범위 (%).
SCALE_MIN = 50
SCALE_MAX = 250

#: 배율 100%일 때의 그림 높이 (논리 픽셀).
BASE_IMAGE_HEIGHT = 360


class ArenaMatchTab(ArenaTab):
    """이상형 월드컵."""

    KEY = "arena"

    def __init__(self, i18n, settings, service, parent: QWidget | None = None) -> None:
        super().__init__(i18n, settings, service, parent)
        self._pair: tuple[Combo, Combo] | None = None
        self._last_pair: tuple[str, str] | None = None
        self._announced_settled = False

        layout = QVBoxLayout(self)

        # ── 위: 리그 · 진행 상황 · 배율 ──────────────────────────────────
        top = QHBoxLayout()
        self.league_label = QLabel()
        top.addWidget(self.league_label)
        self.league_combo = QComboBox()
        for league in LEAGUES:
            self.league_combo.addItem("", league)
        self.league_combo.currentIndexChanged.connect(self._on_league_changed)
        top.addWidget(self.league_combo)
        self.auto_check = QCheckBox()
        self.auto_check.toggled.connect(self._on_auto_toggled)
        top.addWidget(self.auto_check)
        self.keys_check = QCheckBox()
        self.keys_check.toggled.connect(self._on_keys_toggled)
        top.addWidget(self.keys_check)
        top.addStretch(1)
        self.scale_label = QLabel()
        top.addWidget(self.scale_label)
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(SCALE_MIN, SCALE_MAX)
        self.scale_slider.setFixedWidth(140)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)
        top.addWidget(self.scale_slider)
        layout.addLayout(top)

        # 진행 — 이 리그가 얼마나 확정됐는지. 창 상태줄은 전체 숫자를 보여 주므로
        # 여기서는 **지금 보고 있는 리그** 기준으로 얼마나 남았는지를 말한다.
        progress_row = QHBoxLayout()
        self.settled_bar = QProgressBar()
        self.settled_bar.setFixedWidth(140)
        self.settled_bar.setFormat("%v / %m")
        progress_row.addWidget(self.settled_bar)
        self.progress_label = QLabel()
        progress_row.addWidget(self.progress_label, 1)
        layout.addLayout(progress_row)

        # ── 가운데: 두 장, 또는 왜 겨룰 수 없는지 ───────────────────────
        self.cards_stack = QStackedWidget()
        self.cards_page = QWidget()
        cards = QHBoxLayout(self.cards_page)
        cards.setContentsMargins(0, 0, 0, 0)
        self.left_card = ComboCard(i18n, self)
        self.right_card = ComboCard(i18n, self)
        for card, result in ((self.left_card, RESULT_A), (self.right_card, RESULT_B)):
            cards.addWidget(card, 1)
            card.chosen.connect(lambda r=result: self.decide(r))
            card.zoom_requested.connect(lambda c=card: self._zoom(c))
            card.favorite_toggled.connect(lambda c=card: self._toggle_favorite(c))
            card.lock_toggled.connect(lambda c=card: self._toggle_lock(c))
            card.reroll_requested.connect(lambda c=card: self._reroll(c))
            card.delete_requested.connect(lambda c=card: self._delete([c.combo]))
            card.copy_requested.connect(lambda c=card: self._copy(c))
        self.left_card.set_select_key("←", before=True)
        self.right_card.set_select_key("→", before=False)
        self.cards_stack.addWidget(self.cards_page)

        # 겨룰 수 없을 때는 카드 대신 **왜 그런지와 어떻게 푸는지**를 띄운다.
        self.empty_state = EmptyState()
        self.empty_state.action_clicked.connect(self._on_empty_action)
        self.cards_stack.addWidget(self.empty_state)
        layout.addWidget(self.cards_stack, 1)

        #: 빈 화면의 행동 버튼이 무엇을 할지 (`STEP_*` 또는 `_ACTION_ALL_LEAGUE`).
        self._empty_action = ""

        # ── 아래: 양쪽에 대한 판정 ──────────────────────────────────────
        actions = QHBoxLayout()
        self.both_win_button = QPushButton()
        self.both_win_button.clicked.connect(lambda: self.decide(RESULT_BOTH_WIN))
        actions.addWidget(self.both_win_button)
        self.draw_button = QPushButton()
        self.draw_button.clicked.connect(lambda: self.decide(RESULT_DRAW))
        actions.addWidget(self.draw_button)
        self.both_lose_button = QPushButton()
        self.both_lose_button.clicked.connect(lambda: self.decide(RESULT_BOTH_LOSE))
        actions.addWidget(self.both_lose_button)
        self.skip_button = QPushButton()
        self.skip_button.clicked.connect(self.skip)
        actions.addWidget(self.skip_button)
        actions.addStretch(1)
        # 되돌릴 수는 있지만 두 조합이 한 번에 사라진다 — 판정 버튼들과 떼어 놓고
        # 위험 등급을 준다. 예전에는 `넘기기` 바로 옆에 같은 모양으로 있었다.
        self.delete_both_button = QPushButton()
        mark_danger(self.delete_both_button)
        self.delete_both_button.clicked.connect(self._delete_both)
        actions.addWidget(self.delete_both_button)
        layout.addLayout(actions)

        self._shortcuts = self._build_shortcuts()

        self._load_settings()
        self.retranslate()

    # ── ArenaTab 계약 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._pair is None or not self._pair_is_valid():
            self.next_match()
        else:
            self._show_pair()
        self._refresh_progress()

    def commit(self) -> None:
        self.arena.league = self.league_combo.currentData() or LEAGUE_ALL
        self.arena.image_scale = self.scale_slider.value()
        self.arena.auto_prefetch = self.auto_check.isChecked()
        self.arena.match_shortcuts = self.keys_check.isChecked()

    def retranslate(self) -> None:
        tr = self.tr
        for index in range(self.league_combo.count()):
            self.league_combo.setItemText(index, tr(f"arena.league_{self.league_combo.itemData(index)}"))
        self.league_label.setText(tr("arena.league"))
        self.scale_label.setText(tr("arena.image_scale"))
        self.auto_check.setText(tr("arena.auto_prefetch"))
        self.auto_check.setToolTip(tr("arena.auto_prefetch_hint"))
        self.keys_check.setText(tr("arena.match_shortcuts"))
        self.keys_check.setToolTip(tr("arena.match_shortcuts_hint"))
        self._apply_judge_labels()
        self.left_card.retranslate()
        self.right_card.retranslate()
        self.refresh()

    def _apply_judge_labels(self) -> None:
        """판정 버튼 라벨 — 단축키를 켜 두면 키를 라벨 안에 함께 적는다.

        예전에는 `← → 승자 · ↑ 둘 다 좋음 …` 안내문 한 줄이 버튼 아래에 따로 있었다.
        키를 끄면 표기도 사라져야 한다 — 없는 키를 적어 두면 거짓말이 된다.
        """
        tr = self.tr
        keys_on = self.keys_check.isChecked()

        def label(text_key: str, key: str) -> str:
            text = tr(text_key)
            return f"{key}  {text}" if keys_on and key else text

        self.both_win_button.setText(label("arena.both_win", _JUDGE_KEYS[RESULT_BOTH_WIN]))
        self.draw_button.setText(tr("arena.draw"))
        self.both_lose_button.setText(label("arena.both_lose", _JUDGE_KEYS[RESULT_BOTH_LOSE]))
        self.skip_button.setText(label("arena.skip", tr("arena.key_space")))
        self.delete_both_button.setText(label("arena.delete_both", tr("arena.key_delete")))
        self.left_card.set_select_key("←" if keys_on else "", before=True)
        self.right_card.set_select_key("→" if keys_on else "", before=False)

    def on_arena_event(self, event) -> None:
        """그림이 새로 나왔는데 화면이 비어 있으면 바로 올린다."""
        if isinstance(event, ArenaImageReady) and self._pair is None:
            self.next_match()

    # ── 대진 ────────────────────────────────────────────────────────────

    def league_combos(self) -> list[Combo]:
        """지금 리그에 해당하는, 그림이 있는 조합들."""
        combos = ready_combos(self.state)
        league = self.league_combo.currentData() or LEAGUE_ALL
        if league == LEAGUE_FAVORITES:
            return [combo for combo in combos if combo.favorite]
        if league == LEAGUE_UNSETTLED:
            return unsettled(combos)
        return combos

    def next_match(self) -> bool:
        """다음 대진을 골라 화면에 올린다. 올릴 게 없으면 False."""
        pair = pick_match(self.league_combos(), avoid=self._last_pair)
        self._pair = pair
        self._show_pair()
        if pair is None:
            self._maybe_prefetch()
        return pair is not None

    def _pair_is_valid(self) -> bool:
        """화면에 올린 두 조합이 아직 살아 있고 그림도 그대로인지."""
        if self._pair is None:
            return False
        return all(self.state.combo(combo.id) is not None and combo.has_image for combo in self._pair)

    def _show_pair(self) -> None:
        use_prefix = self.arena.use_prefix
        if self._pair is None:
            self.left_card.set_combo(None, None)
            self.right_card.set_combo(None, None)
            self._set_actions_enabled(False)
            self._refresh_empty_state()
            self.cards_stack.setCurrentWidget(self.empty_state)
            return
        for card, combo in zip((self.left_card, self.right_card), self._pair, strict=True):
            card.set_combo(combo, combo_pixmap(self._service.store, combo), use_prefix)
        self._set_actions_enabled(True)
        self.cards_stack.setCurrentWidget(self.cards_page)

    def empty_reason(self) -> tuple[str, str, str]:
        """겨룰 수 없는 이유 — (제목 키, 이유 문구, 행동). 행동은 `STEP_*` 또는 리그 되돌리기.

        원인이 넷이고 푸는 방법이 다 다르다 — "겨룰 그림이 없습니다" 한 줄로는
        무엇을 해야 하는지 알 수 없다. 순서는 막히는 곳이 먼저다.
        """
        tr = self.tr
        ready = len(ready_combos(self.state))
        pending = len(pending_combos(self.state))

        if not self.state.combos:
            return "arena.empty_no_combos", tr("arena.empty_no_combos_detail"), STEP_MAKE_COMBOS
        if ready < MIN_COMBOS_TO_MATCH and pending:
            return (
                "arena.empty_no_images",
                tr("arena.empty_no_images_detail").format(pending),
                STEP_GENERATE,
            )
        if ready < MIN_COMBOS_TO_MATCH:
            return (
                "arena.empty_need_more",
                tr("arena.empty_need_more_detail").format(ready),
                STEP_MAKE_COMBOS,
            )
        # 그림은 넉넉한데 리그를 좁혀 놓아 후보가 둘이 안 된다 — 여기서 바로 풀 수 있다.
        return "arena.empty_league", tr("arena.empty_league_detail"), _ACTION_ALL_LEAGUE

    def _refresh_empty_state(self) -> None:
        title, detail, action = self.empty_reason()
        self._empty_action = action
        self.empty_state.set_content(self.tr(title), detail, self.tr(_ACTION_LABELS[action]))

    def _on_empty_action(self) -> None:
        if self._empty_action == _ACTION_ALL_LEAGUE:
            self.league_combo.setCurrentIndex(self.league_combo.findData(LEAGUE_ALL))
            return
        if self._empty_action:
            self.navigate_requested.emit(self._empty_action)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in (
            self.both_win_button,
            self.draw_button,
            self.both_lose_button,
            self.skip_button,
            self.delete_both_button,
        ):
            button.setEnabled(enabled)

    def _refresh_progress(self) -> None:
        """이 리그가 얼마나 확정됐는지. 전체 숫자는 창 상태줄이 이미 보여 준다."""
        league = self.league_combos()
        settled = sum(1 for combo in league if is_settled(combo.matches))
        self.settled_bar.setRange(0, max(1, len(league)))
        self.settled_bar.setValue(settled)
        self.progress_label.setText(
            self.tr("arena.league_progress").format(self.state.total_matches, len(league), settled)
        )

        ready = len(ready_combos(self.state))
        left = len(unsettled(self.state.combos))
        if ready >= 2 and left == 0 and not self._announced_settled:
            # 전부 확정됐다는 것은 "이제 진화 탭으로 갈 때"라는 뜻이다.
            self.status_message.emit(self.tr("arena.all_settled"))
            self._announced_settled = True
        elif left > 0:
            self._announced_settled = False

    # ── 판정 ────────────────────────────────────────────────────────────

    def decide(self, result: str) -> bool:
        """대결 결과를 반영하고 다음 대진으로 넘어간다."""
        if self._pair is None:
            return False
        left, right = self._pair
        apply_match(self.state, left, right, result)
        self._last_pair = (left.id, right.id)
        self._service.save()
        self.state_changed.emit()
        self.next_match()
        self._refresh_progress()
        return True

    def skip(self) -> bool:
        """점수를 건드리지 않고 다음 대진으로. 판단이 안 설 때 쓴다."""
        if self._pair is None:
            return False
        self._last_pair = (self._pair[0].id, self._pair[1].id)
        return self.next_match()

    # ── 카드 조작 ───────────────────────────────────────────────────────

    def _toggle_favorite(self, card: ComboCard) -> None:
        if card.combo is None:
            return
        card.combo.favorite = card.favorite_button.isChecked()
        self._service.save()
        self.state_changed.emit()

    def _toggle_lock(self, card: ComboCard) -> None:
        if card.combo is None:
            return
        card.combo.locked = card.lock_button.isChecked()
        self._service.save()
        self.state_changed.emit()

    def _reroll(self, card: ComboCard) -> None:
        """작가는 그대로 두고 가중치만 새로 뽑는다. 그림은 다시 뽑아야 하므로 지운다.

        점수도 되돌린다 — 가중치가 달라졌으면 그건 사실상 다른 그림체라, 예전 전적을
        물려주면 순위가 거짓말이 된다.
        """
        combo = card.combo
        if combo is None:
            return
        # 리롤은 이 조합을 계속 쓰겠다는 뜻이라 그림을 실제로 지운다 — 같은 조합의
        # 옛 그림이 폴더에 남아 봐야 쓸 데가 없다.
        self._service.store.delete_images([combo])
        combo.slots = regenerate_weights(combo, self.arena.combo_params(), self.state.artists)
        combo.image_path = ""
        combo.elo = DEFAULT_ELO
        combo.matches = 0
        combo.wins = 0
        self._service.save()
        self.status_message.emit(self.tr("arena.rerolled"))
        self.state_changed.emit()
        self.next_match()

    def _copy(self, card: ComboCard) -> None:
        if card.combo is None:
            return
        block = format_artist_block(card.combo.slots, self.arena.use_prefix)
        QGuiApplication.clipboard().setText(block)
        self.status_message.emit(self.tr("arena.copied"))

    def _delete(self, combos: list[Combo | None]) -> None:
        """조합을 지운다 — 서비스를 거치므로 `되돌리기`로 되살릴 수 있다."""
        removed = self._service.remove_combos(
            [combo for combo in combos if combo is not None],
            delete_images=self.arena.delete_images_with_combo,
        )
        if not removed:
            return
        self.status_message.emit(self.tr("arena.deleted_undoable").format(removed))
        self.state_changed.emit()
        self.next_match()

    def _delete_both(self) -> None:
        if self._pair is not None:
            self._delete(list(self._pair))

    def _zoom(self, card: ComboCard) -> None:
        """그림을 큰 창으로 띄운다 — 클릭은 고르는 것이 아니라 보는 것이다."""
        if card.pixmap is None:
            return
        window = QDialog(self)
        window.setWindowTitle(self.tr("arena.zoom_title"))
        layout = QVBoxLayout(window)
        view = ZoomableImageView(window)
        view.setPixmap(card.pixmap)
        layout.addWidget(view)
        window.resize(900, 900)
        window.show()

    # ── 설정·입력 ───────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        arena = self.arena
        index = self.league_combo.findData(arena.league)
        if index >= 0:
            self.league_combo.setCurrentIndex(index)
        self.scale_slider.setValue(max(SCALE_MIN, min(SCALE_MAX, arena.image_scale)))
        self.auto_check.setChecked(arena.auto_prefetch)
        self.keys_check.setChecked(arena.match_shortcuts)
        self._apply_shortcut_state()
        self._apply_scale()

    def _on_league_changed(self) -> None:
        self.arena.league = self.league_combo.currentData() or LEAGUE_ALL
        self._pair = None
        self.refresh()

    def _on_scale_changed(self) -> None:
        self.arena.image_scale = self.scale_slider.value()
        self._apply_scale()

    def _on_auto_toggled(self, checked: bool) -> None:
        self.arena.auto_prefetch = checked
        if checked:
            # 방금 사용자가 켰다 — 이 탭을 보고 있는 것이 확실하다.
            self._maybe_prefetch(from_user=True)

    def reset_image_scale(self) -> None:
        """배율을 100%로 되돌린다 — 창 크기 초기화가 함께 부른다.

        배율을 크게 해 두면 카드가 커져 창도 함께 커진다. 창만 줄여 놓으면
        다음에 열 때 또 커지므로 여기까지 같이 되돌린다.
        """
        self.scale_slider.setValue(100)

    def _apply_scale(self) -> None:
        height = round(BASE_IMAGE_HEIGHT * self.scale_slider.value() / 100)
        self.left_card.set_image_height(height)
        self.right_card.set_image_height(height)

    def _maybe_prefetch(self, from_user: bool = False) -> None:
        """그림이 부족하면 더 뽑는다 — **켜 둔 경우에만**.

        기본이 꺼짐인 이유는 이것이 실제로 크레딧을 쓰는 동작이기 때문이다. 사용자가
        누르지 않았는데 앱이 알아서 소모하면 안 된다. 꺼져 있으면 안내만 한다.

        `from_user`는 사용자가 방금 `그림 자동 보충`을 켠 경우다. 그때는 이 탭을 보고
        있는 것이 확실하므로 아래의 "다른 탭" 가드를 건너뛴다.
        """
        pending = pending_combos(self.state)
        if not pending:
            return
        if self._service.is_running:
            # 이미 뽑는 중이다. 여기서 "그림이 없다"를 띄우면 상태줄의 생성 진행
            # 표시를 덮어 버린다 — 곧 생긴다.
            return
        if not from_user and self.isHidden():
            # **다른 탭을 보고 있다 — 아무것도 하지 않는다.**
            #
            # 이 탭은 조합·작가가 바뀔 때마다 다시 그려진다. 그래서 이 가드가 없으면
            # 조합 생성 탭에서 `랜덤 조합 만들기`를 누르는 것만으로 여기까지 흘러와
            # **크레딧을 쓰는 생성이 저절로 시작됐다** (자동 보충을 켜 둔 경우).
            # 조합을 여러 번 나눠 만들 수도 없었다 — 첫 묶음에서 바로 생성이 걸렸다.
            #
            # 안내 메시지도 마찬가지다. 여기서 띄우면 방금 그 탭에서 한 일의 결과를
            # 덮는다 (조합을 지웠는데 "겨룰 그림이 없습니다"가 뜨는 식으로).
            return
        if self.auto_check.isChecked():
            if self._service.needs_prefetch(max(2, self.arena.prefetch_threshold)):
                self.request_prefetch.emit()
            return
        self.status_message.emit(self.tr("arena.need_images").format(len(pending)))

    def _on_keys_toggled(self, checked: bool) -> None:
        self.arena.match_shortcuts = checked
        self._apply_shortcut_state()
        self.status_message.emit(
            self.tr("arena.match_shortcuts_on" if checked else "arena.match_shortcuts_off")
        )

    # ── 판정 단축키 ─────────────────────────────────────────────────────
    #
    # 탭의 `keyPressEvent`는 **탭 자신이 포커스를 가졌을 때만** 불린다. 실제로는
    # 그림이나 단추를 한 번 누르는 순간 포커스가 그 위젯으로 넘어가, 방향키는
    # 슬라이더가, `Space`는 단추가 먼저 먹어 버린다 — 그래서 안내에 적힌 키가
    # 사실상 동작하지 않았다.
    #
    # `QShortcut`은 포커스 위젯보다 **먼저** 키를 본다. 범위를
    # `WidgetWithChildrenShortcut`으로 묶어, 이 탭 안에 포커스가 있을 때만 살게
    # 한다 — 다른 탭의 숫자 상자나 메인 창은 그대로 방향키를 쓴다.

    def _key_actions(self) -> dict:
        return {
            Qt.Key.Key_Left: lambda: self.decide(RESULT_A),
            Qt.Key.Key_Right: lambda: self.decide(RESULT_B),
            Qt.Key.Key_Up: lambda: self.decide(RESULT_BOTH_WIN),
            Qt.Key.Key_Down: lambda: self.decide(RESULT_BOTH_LOSE),
            Qt.Key.Key_Space: self.skip,
            Qt.Key.Key_Delete: self._delete_both,
        }

    def _build_shortcuts(self) -> list[QShortcut]:
        shortcuts = []
        for key in self._key_actions():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda k=key: self.handle_shortcut(k))
            shortcuts.append(shortcut)
        return shortcuts

    def shortcuts_enabled(self) -> bool:
        """`단축키 사용` 체크 상태 (설정에 남는 값)."""
        return self.keys_check.isChecked()

    def _apply_shortcut_state(self) -> None:
        enabled = self.shortcuts_enabled()
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)
        # 라벨에 적힌 키도 함께 사라진다 — 안 먹는 키를 적어 두면 거짓말이 된다.
        self._apply_judge_labels()

    def handle_shortcut(self, key: int) -> bool:
        """판정 키면 처리하고 True. 아니면 False (원래 위젯이 받게 둔다)."""
        action = self._key_actions().get(key)
        if action is None or not self.shortcuts_enabled() or self._pair is None:
            return False
        action()
        return True

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        """탭 자신이 포커스를 가진 경우 — 보통은 위의 단축키가 먼저 받는다."""
        if self.handle_shortcut(event.key()):
            return
        super().keyPressEvent(event)


__all__ = [
    "BASE_IMAGE_HEIGHT",
    "LEAGUES",
    "LEAGUE_ALL",
    "LEAGUE_FAVORITES",
    "LEAGUE_UNSETTLED",
    "ArenaMatchTab",
]
