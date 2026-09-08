"""조합 카드 — 그림 한 장과 그 조합의 정보·조작 버튼 (월드컵·진화 공용).

**그림을 클릭하면 확대해서 본다** — 고르지 않는다. 사용자 제보 중에 "자세히 보려고
눌렀는데 그 그림이 마음에 든 것으로 처리돼 곤란하다"는 것이 있었고, 클릭=확대가
일반적인 UI 동작이기도 하다. 고르는 것은 그림 아래의 선택 버튼과 방향키로 한다.

같은 제보 중에 "선택 버튼이 크기만 크고 자리를 차지해 그림이 작아진다"는 것도 있어,
선택 버튼은 그림 폭에 맞춘 한 줄로 얇게 둔다 (없애지는 않는다 — 없으면 고를 방법이
클릭밖에 남지 않아 위의 문제로 되돌아간다).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.combos import format_artist_block
from ...core.arena.elo import is_settled, tier_of
from ...core.arena.models import Combo
from ...core.i18n.manager import I18nManager
from ..widgets.hidpi_image import HiDpiImageLabel

logger = logging.getLogger(__name__)

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

        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)

        self.header_label = QLabel()
        layout.addWidget(self.header_label)

        self.image_label = _ClickableImage()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image_label.clicked.connect(self.zoom_requested)
        layout.addWidget(self.image_label, 1)

        self.select_button = QPushButton()
        self.select_button.clicked.connect(self.chosen)
        layout.addWidget(self.select_button)

        self.prompt_label = QLabel()
        self.prompt_label.setWordWrap(True)
        self.prompt_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.prompt_label)

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
        self.delete_button.clicked.connect(self.delete_requested)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        self.retranslate()

    # ── 내용 ────────────────────────────────────────────────────────────

    @property
    def combo(self) -> Combo | None:
        return self._combo

    def set_combo(self, combo: Combo | None, image_path: str | None, use_prefix: bool = True) -> None:
        """카드에 조합을 앉힌다. `image_path`가 None이면 그림 자리에 안내를 띄운다."""
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

        if image_path:
            pixmap = QPixmap(image_path)
            if not pixmap.isNull():
                self._pixmap = pixmap
                self._draw_image()
                return
            logger.debug("cannot load arena image: %s", image_path)
        self.image_label.setText(self._i18n.get_text("arena.card_no_image"))

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
        return tr("arena.card_header").format(combo.generation, tier, round(combo.elo), state)

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

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._draw_image()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.select_button.setText(tr("arena.card_select"))
        self.favorite_button.setText(tr("arena.card_favorite"))
        self.lock_button.setText(tr("arena.card_lock"))
        self.reroll_button.setText(tr("arena.card_reroll"))
        self.copy_button.setText(tr("arena.card_copy"))
        self.delete_button.setText(tr("arena.card_delete"))
        self.favorite_button.setToolTip(tr("arena.card_favorite_hint"))
        self.lock_button.setToolTip(tr("arena.card_lock_hint"))
        self.reroll_button.setToolTip(tr("arena.card_reroll_hint"))
        if self._combo is not None:
            self.header_label.setText(self._header_text(self._combo))


__all__ = ["MIN_IMAGE_HEIGHT", "ComboCard"]
