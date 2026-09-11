"""조합 카드 — 그림 한 장과 그 조합의 정보·조작 (월드컵·진화 공용).

**그림을 클릭하면 확대해서 본다** — 고르지 않는다. 사용자 제보 중에 "자세히 보려고
눌렀는데 그 그림이 마음에 든 것으로 처리돼 곤란하다"는 것이 있었고, 클릭=확대가
일반적인 UI 동작이기도 하다. 고르는 것은 그림 아래의 선택 버튼과 방향키로 한다.

## 버튼을 어디에 두는가

여섯 개(선택·즐겨찾기·잠금·리롤·복사·삭제)가 한 줄에 같은 크기·같은 톤으로 있어서,
되돌릴 수 없는 `삭제`가 `복사` 옆에 똑같이 생긴 채로 놓여 있었다. 무게만 나눈다.

- **선택** — 카드에서 제일 큰 버튼. 이 화면의 주 동작이다.
- **나머지 다섯** — 그 아래 한 줄에 **전부 보인다.** 다만 `삭제`는 오른쪽 끝으로 떼어
  놓고 위험 등급(붉은 글씨·테두리)을 준다.
- 같은 항목을 **우클릭 메뉴**에도 둔다 (크게 보기 포함).

> 한때 리롤·복사·삭제를 그림 위에 겹쳐 마우스를 올렸을 때만 뜨게 했는데 되돌렸다.
> Qt는 커서가 자식 위젯(여기서는 그림 라벨)으로 들어가는 순간 부모에게 `leaveEvent`를
> 보낸다 — 그림 쪽으로 다가가는 것만으로 아이콘 줄이 사라져, 사실상 못 누르는
> 버튼이 됐다. 버튼은 보이는 자리에 둔다.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import format_artist_block
from ...core.arena.elo import is_settled, tier_of
from ...core.arena.models import Combo
from ...core.i18n.manager import I18nManager
from ..widgets.hidpi_image import HiDpiImageLabel
from .style import mark_danger, mark_primary, tier_color

#: 그림 자리가 줄어들 수 있는 하한. 이보다 아래로는 무엇을 보고 고르는지 알 수 없다.
MIN_IMAGE_HEIGHT = 160


class _ClickableImage(HiDpiImageLabel):
    """클릭하면 신호를 내는 이미지 라벨."""

    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ComboCard(QFrame):
    """조합 하나를 보여 주는 카드."""

    chosen = Signal()  # 이 조합을 골랐다 (월드컵 승자)
    zoom_requested = Signal()  # 크게 보기
    favorite_toggled = Signal()
    lock_toggled = Signal()
    reroll_requested = Signal()  # 가중치만 새로 뽑아 다시 그린다
    delete_requested = Signal()
    copy_requested = Signal()  # 프롬프트를 클립보드로

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._combo: Combo | None = None
        self._pixmap: QPixmap | None = None
        self._image_height = 360
        #: 선택 버튼에 붙일 단축키 표시 — (기호, 글 앞에 붙일지). 빈 기호면 안 붙인다.
        self._select_key = ("", True)

        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)

        self.header_label = QLabel()
        # 티어 글자에만 색을 주려면 서식 있는 글이어야 한다.
        self.header_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.header_label)

        self.image_label = _ClickableImage()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image_label.clicked.connect(self.zoom_requested)
        layout.addWidget(self.image_label, 1)

        self.select_button = QPushButton()
        mark_primary(self.select_button)
        self.select_button.clicked.connect(self.chosen)
        layout.addWidget(self.select_button)

        self.prompt_label = QLabel()
        self.prompt_label.setWordWrap(True)
        self.prompt_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.prompt_label)

        # 조작 한 줄 — 전부 보인다. `삭제`만 오른쪽 끝으로 떼어 놓는다.
        actions = QHBoxLayout()
        self.favorite_button = QPushButton()
        self.favorite_button.setCheckable(True)
        self.favorite_button.clicked.connect(self.favorite_toggled)
        actions.addWidget(self.favorite_button)
        self.lock_button = QPushButton()
        self.lock_button.setCheckable(True)
        self.lock_button.clicked.connect(self.lock_toggled)
        actions.addWidget(self.lock_button)
        self.reroll_button = QPushButton()
        self.reroll_button.clicked.connect(self.reroll_requested)
        actions.addWidget(self.reroll_button)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self.copy_requested)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        self.delete_button = QPushButton()
        mark_danger(self.delete_button)
        self.delete_button.clicked.connect(self.delete_requested)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.retranslate()

    # ── 내용 ────────────────────────────────────────────────────────────

    @property
    def combo(self) -> Combo | None:
        return self._combo

    def set_combo(self, combo: Combo | None, pixmap: QPixmap | None, use_prefix: bool = True) -> None:
        """카드에 조합을 앉힌다. `pixmap`이 None이면 그림 자리에 안내를 띄운다.

        그림 로딩·캐시는 `arena/thumbnails.combo_pixmap`이 맡는다 — 카드는 받은
        픽스맵을 그리기만 한다.
        """
        self._combo = combo
        self._pixmap = None
        if combo is None:
            self.header_label.setText("")
            self.prompt_label.setText("")
            self.image_label.setText(self._i18n.get_text("arena.card_empty"))
            self._set_enabled(False)
            return

        self._set_enabled(True)
        self.header_label.setText(self._header_text(combo))
        self.prompt_label.setText(format_artist_block(combo.slots, use_prefix))
        self.favorite_button.setChecked(combo.favorite)
        self.lock_button.setChecked(combo.locked)

        if pixmap is not None and not pixmap.isNull():
            self._pixmap = pixmap
            self._draw_image()
            return
        self.image_label.setText(self._i18n.get_text("arena.card_no_image"))

    def set_select_key(self, key: str, before: bool = True) -> None:
        """선택 버튼에 단축키를 표시한다 (`←` / `→`). 빈 문자열이면 지운다.

        안내문 한 줄로 "← → 승자"라고 적어 두는 것보다, 누를 버튼 자신이 어떤 키인지
        말하는 편이 눈이 덜 움직인다.
        """
        self._select_key = (key, before)
        self._apply_select_label()

    def set_image_height(self, height: int) -> None:
        """그림 높이(논리 픽셀)를 바꾼다 — 배율 슬라이더가 쓴다.

        고해상도 모니터에서 그림이 너무 작다는 지적이 있어 넣었다.

        **최대**를 정하고 최소는 `MIN_IMAGE_HEIGHT`에 묶어 둔다. 요청한 높이를
        그대로 최소로 걸면 그것이 창 전체의 최소 높이를 밀어 올려, 배율을 크게
        해 둔 채로 낮은 해상도 PC에서 열면 창이 화면보다 커져 잘린다 (제보).
        """
        self._image_height = max(120, height)
        self.image_label.setMinimumHeight(min(self._image_height, MIN_IMAGE_HEIGHT))
        self.image_label.setMaximumHeight(self._image_height)
        self._draw_image()

    @property
    def pixmap(self) -> QPixmap | None:
        """원본 픽스맵 — 크게 보기 창이 쓴다."""
        return self._pixmap

    def _draw_image(self) -> None:
        if self._pixmap is None:
            return
        # 폭도 함께 제한한다 — 가로로 긴 그림이 카드를 밀어내지 않게.
        box = QSize(max(160, self.width() - 32), self._image_height)
        self.image_label.show_fitted(self._pixmap, box)

    def _header_text(self, combo: Combo) -> str:
        tr = self._i18n.get_text
        tier = tier_of(combo.elo)
        if not combo.matches:
            state = tr("arena.card_unrated")
        elif is_settled(combo.matches):
            state = tr("arena.card_settled")
        else:
            state = tr("arena.card_provisional")
        colour = tier_color(self.palette(), tier)
        shown = tier if colour is None else f'<span style="color:{colour}">{tier}</span>'
        return tr("arena.card_header").format(combo.generation, shown, round(combo.elo), state)

    def _apply_select_label(self) -> None:
        text = self._i18n.get_text("arena.card_select")
        key, before = self._select_key
        if key:
            text = f"{key}  {text}" if before else f"{text}  {key}"
        self.select_button.setText(text)

    def _set_enabled(self, enabled: bool) -> None:
        for button in (
            self.select_button,
            self.favorite_button,
            self.lock_button,
            self.reroll_button,
            self.copy_button,
            self.delete_button,
        ):
            button.setEnabled(enabled)

    # ── 우클릭 메뉴 ─────────────────────────────────────────────────────

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        """버튼 줄과 같은 항목 + `크게 보기`. 손이 마우스에 있을 때 더 빠르다."""
        if self._combo is None:
            return
        tr = self._i18n.get_text
        menu = QMenu(self)
        for text, signal in (
            (tr("arena.card_zoom"), self.zoom_requested),
            (tr("arena.card_reroll"), self.reroll_requested),
            (tr("arena.card_copy"), self.copy_requested),
            (tr("arena.card_delete"), self.delete_requested),
        ):
            action = QAction(text, menu)
            action.triggered.connect(signal)
            menu.addAction(action)
        menu.exec(event.globalPos())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._draw_image()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self._apply_select_label()
        self.favorite_button.setText(tr("arena.card_favorite"))
        self.lock_button.setText(tr("arena.card_lock"))
        self.reroll_button.setText(tr("arena.card_reroll"))
        self.copy_button.setText(tr("arena.card_copy"))
        self.delete_button.setText(tr("arena.card_delete"))
        self.favorite_button.setToolTip(tr("arena.card_favorite_hint"))
        self.lock_button.setToolTip(tr("arena.card_lock_hint"))
        self.reroll_button.setToolTip(tr("arena.card_reroll_hint"))
        self.image_label.setToolTip(tr("arena.card_zoom_hint"))
        if self._combo is not None:
            self.header_label.setText(self._header_text(self._combo))


__all__ = ["MIN_IMAGE_HEIGHT", "ComboCard"]
