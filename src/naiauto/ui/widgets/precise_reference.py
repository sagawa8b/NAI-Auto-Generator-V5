"""Precise Reference 패널 — 참조 이미지로 캐릭터·스타일·의상을 가져온다 (V5).

레퍼런스 한 장마다 종류(Character / Style / Character & Style / Outfit)와 강도를 고른다.
종류를 섞어 여러 장 쓸 수 있다 (NovelAI 출시 공지). 장당 5 Anlas가 든다.

i2i 패널처럼 보기 메뉴로 켤 때만 보이고, 꺼져 있으면 이미지를 들고 있어도 요청에
반영하지 않는다 — 숨은 상태가 조용히 payload(그리고 Anlas)를 바꾸지 않도록.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core.api.models import (
    PREFER_SAME_OUTFIT_DEFAULTS,
    REFERENCE_TYPES,
    CharacterReference,
    ReferenceType,
)
from ...core.i18n.manager import I18nManager
from .hidpi_image import HiDpiImageLabel
from .wheel_guard import WheelGuard, guard_wheel

THUMB_HEIGHT = 72
#: NovelAI 공지: "5 Anlas per reference image"
ANLAS_PER_REFERENCE = 5
#: V5 강도 범위 — 웹 UI 슬라이더가 1.0을 넘어 5.0까지 간다 (출시 공지)
STRENGTH_MAX = 5.0
#: 새 레퍼런스의 기본 강도. 공지가 "꽤 높게" 두면 잘 듣는다고 권해 1.0으로 시작한다.
DEFAULT_STRENGTH = 1.0
DEFAULT_TYPE: ReferenceType = "character"
#: 슬라이더는 정수만 다루므로 강도를 이 배율로 옮긴다 (0.01 단위 — 스핀박스 소수 둘째 자리와 같다)
SLIDER_SCALE = 100


def to_png(data: bytes) -> bytes:
    """PNG가 아니면 PNG로 바꾼다 (투명도는 보존 — 레터박스가 검은 배경에 얹는다)."""
    with Image.open(io.BytesIO(data)) as img:
        if img.format == "PNG":
            return data
        buf = io.BytesIO()
        img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buf, format="PNG")
        return buf.getvalue()


@dataclass
class _Entry:
    image: bytes
    row: ReferenceRow


class ReferenceRow(QFrame):
    """레퍼런스 한 장: 썸네일 · 종류 · 강도 · 제거."""

    changed = Signal()
    remove_requested = Signal(object)

    def __init__(self, i18n: I18nManager, image: bytes, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.thumbnail = HiDpiImageLabel()
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setFixedWidth(THUMB_HEIGHT)
        pixmap = QPixmap()
        pixmap.loadFromData(image)
        self.thumbnail.show_at_height(pixmap, THUMB_HEIGHT)
        layout.addWidget(self.thumbnail)

        controls = QVBoxLayout()
        self.type_combo = QComboBox()
        for ref_type in REFERENCE_TYPES:
            self.type_combo.addItem("", userData=ref_type)
        self.type_combo.setCurrentIndex(self.type_combo.findData(DEFAULT_TYPE))
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        controls.addWidget(self.type_combo)

        # Prefer same outfit — 캐릭터 · 캐릭터&스타일에만 있다. 웹 UI처럼 종류를 바꾸면 그
        # 종류의 기본값(캐릭터 끔, 캐릭터&스타일 켬)으로 돌아간다.
        self.outfit_check = QCheckBox()
        self.outfit_check.toggled.connect(lambda _on: self.changed.emit())
        controls.addWidget(self.outfit_check)
        self._sync_outfit_check()

        # 강도 — 웹 UI처럼 슬라이더로 끌고, 정확한 값은 옆 칸에 직접 넣는다. 둘은 같은 값을 본다.
        strength_row = QHBoxLayout()
        self.strength_label = QLabel()
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(0, round(STRENGTH_MAX * SLIDER_SCALE))
        self.strength_slider.setSingleStep(5)  # 방향키 한 번 = 0.05
        self.strength_slider.setPageStep(50)  # PgUp/PgDn·홈 클릭 = 0.5
        self.strength_spin = QDoubleSpinBox()
        self.strength_spin.setRange(0.0, STRENGTH_MAX)
        self.strength_spin.setSingleStep(0.1)
        self.strength_spin.setDecimals(2)
        self.strength_spin.setFixedWidth(64)
        self.strength_slider.valueChanged.connect(self._on_slider_moved)
        self.strength_spin.valueChanged.connect(self._on_spin_changed)
        self.strength_spin.setValue(DEFAULT_STRENGTH)
        strength_row.addWidget(self.strength_label)
        strength_row.addWidget(self.strength_slider, stretch=1)
        strength_row.addWidget(self.strength_spin)
        controls.addLayout(strength_row)
        layout.addLayout(controls, stretch=1)

        self.remove_button = QPushButton("✕")
        self.remove_button.setFixedWidth(28)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.remove_button, alignment=Qt.AlignmentFlag.AlignTop)

    @property
    def ref_type(self) -> ReferenceType:
        return self.type_combo.currentData()

    @property
    def prefer_same_outfit(self) -> bool | None:
        """토글 값. 토글이 없는 종류(스타일 · 의상)면 None."""
        if self.ref_type not in PREFER_SAME_OUTFIT_DEFAULTS:
            return None
        return self.outfit_check.isChecked()

    def set_prefer_same_outfit(self, value: bool | None) -> None:
        """None이면 지금 종류의 기본값으로 둔다."""
        default = PREFER_SAME_OUTFIT_DEFAULTS.get(self.ref_type)
        if default is not None:
            self.outfit_check.setChecked(default if value is None else value)

    def _on_type_changed(self, _index: int) -> None:
        self._sync_outfit_check()
        self.changed.emit()

    def _sync_outfit_check(self) -> None:
        default = PREFER_SAME_OUTFIT_DEFAULTS.get(self.ref_type)
        self.outfit_check.setVisible(default is not None)
        if default is not None:
            self.outfit_check.blockSignals(True)  # 종류 변경의 changed 한 번으로 충분하다
            self.outfit_check.setChecked(default)
            self.outfit_check.blockSignals(False)

    def set_ref_type(self, ref_type: ReferenceType) -> None:
        index = self.type_combo.findData(ref_type)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)

    @property
    def strength(self) -> float:
        return self.strength_spin.value()

    def _on_slider_moved(self, position: int) -> None:
        # 스핀박스가 값의 주인이다 — 슬라이더는 그 값을 옮겨 적고, changed는 스핀박스가 낸다
        self.strength_spin.setValue(position / SLIDER_SCALE)

    def _on_spin_changed(self, value: float) -> None:
        position = round(value * SLIDER_SCALE)
        if self.strength_slider.value() != position:
            self.strength_slider.blockSignals(True)
            self.strength_slider.setValue(position)
            self.strength_slider.blockSignals(False)
        self.changed.emit()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        for i in range(self.type_combo.count()):
            self.type_combo.setItemText(
                i, tr(f"precise_reference.type_{_type_key(self.type_combo.itemData(i))}")
            )
            self.type_combo.setItemData(
                i,
                tr(f"precise_reference.type_{_type_key(self.type_combo.itemData(i))}_tooltip"),
                Qt.ItemDataRole.ToolTipRole,
            )
        self.strength_label.setText(tr("precise_reference.strength"))
        self.outfit_check.setText(tr("precise_reference.prefer_same_outfit"))
        self.outfit_check.setToolTip(tr("precise_reference.prefer_same_outfit_tooltip"))
        self.strength_spin.setToolTip(tr("precise_reference.strength_tooltip"))
        self.strength_slider.setToolTip(tr("precise_reference.strength_tooltip"))
        self.remove_button.setToolTip(tr("precise_reference.remove"))


def _type_key(ref_type: str) -> str:
    """i18n 키 조각 — "character&style"에는 키에 쓸 수 없는 &가 있다."""
    return {"character&style": "character_style", "costume": "outfit"}.get(ref_type, ref_type)


class PreciseReferencePanel(QGroupBox):
    """레퍼런스 목록을 보관하고 요청용 `CharacterReference` 튜플로 내놓는다."""

    changed = Signal()

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._entries: list[_Entry] = []
        self._active = False
        self._supported = True
        self._wheel_guard: WheelGuard | None = None

        layout = QVBoxLayout(self)
        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.empty_label)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(4)
        layout.addLayout(self.rows_layout)

        buttons = QHBoxLayout()
        self.add_button = QPushButton()
        self.add_button.clicked.connect(self.choose_images)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self.clear)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self.cost_label = QLabel()
        self.cost_label.setWordWrap(True)
        self.cost_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.cost_label)

        self._refresh()

    # ── 상태 ─────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        """보기 메뉴 토글. 꺼지면 숨기고 요청에도 반영하지 않는다."""
        self._active = active
        self.setVisible(active)
        self.changed.emit()

    def set_supported(self, supported: bool) -> None:
        """현재 모델이 Precise Reference를 지원하는지. 미지원이면 요청에 넣지 않는다."""
        self._supported = supported
        self.setEnabled(supported)
        self._refresh()
        self.changed.emit()

    @property
    def rows(self) -> list[ReferenceRow]:
        return [e.row for e in self._entries]

    def references(self) -> tuple[CharacterReference, ...]:
        """요청에 넣을 레퍼런스. 패널이 꺼져 있거나 모델이 지원하지 않으면 비어 있다."""
        if not (self._active and self._supported):
            return ()
        return tuple(
            CharacterReference(
                image=e.image,
                type=e.row.ref_type,
                strength=e.row.strength,
                prefer_same_outfit=e.row.prefer_same_outfit,
            )
            for e in self._entries
        )

    def share_wheel_guard(self, guard: WheelGuard) -> None:
        """나중에 추가하는 레퍼런스 줄에도 메인 창의 휠 가드를 건다 (스크롤 중 강도가 바뀌지 않게)."""
        self._wheel_guard = guard

    # ── 조작 ─────────────────────────────────────────────

    def choose_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, self._i18n.get_text("precise_reference.add"), "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        for path in paths:
            if not self.load_image(path):
                break

    def load_image(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                data = to_png(f.read())
        except Exception as e:
            QMessageBox.warning(
                self,
                self._i18n.get_text("errors.title"),
                self._i18n.get_text("precise_reference.load_failed", e),
            )
            return False
        self.add_reference(data)
        return True

    def add_reference(
        self,
        image: bytes,
        ref_type: ReferenceType = DEFAULT_TYPE,
        strength: float = DEFAULT_STRENGTH,
        prefer_same_outfit: bool | None = None,
    ) -> ReferenceRow:
        row = ReferenceRow(self._i18n, image, self)
        if self._wheel_guard is not None:
            guard_wheel(row, self._wheel_guard)
        row.set_ref_type(ref_type)
        row.set_prefer_same_outfit(prefer_same_outfit)
        row.strength_spin.setValue(strength)
        row.retranslate()
        row.changed.connect(self._on_row_changed)
        row.remove_requested.connect(self.remove_row)
        self._entries.append(_Entry(image=image, row=row))
        self.rows_layout.addWidget(row)
        self._refresh()
        self.changed.emit()
        return row

    def remove_row(self, row: ReferenceRow) -> None:
        self._entries = [e for e in self._entries if e.row is not row]
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._refresh()
        self.changed.emit()

    def clear(self) -> None:
        for entry in list(self._entries):
            self.remove_row(entry.row)

    def _on_row_changed(self) -> None:
        self.changed.emit()

    # ── 표시 ─────────────────────────────────────────────

    def _refresh(self) -> None:
        tr = self._i18n.get_text
        count = len(self._entries)
        self.empty_label.setVisible(count == 0)
        self.clear_button.setEnabled(count > 0)
        if not self._supported:
            self.cost_label.setText(tr("precise_reference.unsupported"))
        elif count:
            self.cost_label.setText(tr("precise_reference.cost", count, count * ANLAS_PER_REFERENCE))
        else:
            self.cost_label.setText(tr("precise_reference.cost_note", ANLAS_PER_REFERENCE))

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setTitle(tr("precise_reference.title"))
        self.empty_label.setText(tr("precise_reference.empty"))
        self.add_button.setText(tr("precise_reference.add"))
        self.clear_button.setText(tr("precise_reference.clear"))
        for row in self.rows:
            row.retranslate()
        self._refresh()
