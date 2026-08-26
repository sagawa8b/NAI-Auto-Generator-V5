"""상시 편집 가능한 해상도 패널 (Req 10).

계산은 전부 `core.resolution_catalog`에 있다. 이 모듈은 조립과 배선만 한다.

Aspect 버튼 라벨(`Wide` / `Square` / `Portrait`)은 `Aspect.value` 리터럴을 그대로 쓰고
`retranslate()`에서도 건드리지 않는다 (Req 10.4). NovelAI 웹 UI 용어와 1:1로 대응하는
식별자이므로 번역 대상이 아니다.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.resolution_catalog import (
    DIMENSION_STEP,
    MAX_DIMENSION,
    MIN_DIMENSION,
    Aspect,
    Resolution,
    ResolutionCatalog,
    ResolutionGroup,
    classify_aspect,
    exceeds_free_pixels,
    snap_size,
)
from .wheel_guard import guard_wheel

if TYPE_CHECKING:  # pragma: no cover - 타입 힌트 전용
    from collections.abc import Iterable

DIRECT_ENTRY = "__direct__"  # 등급 콤보의 "직접 입력" 항목 userData (Req 10.10)

DEFAULT_SIZE = (832, 1216)

__all__ = ["DIRECT_ENTRY", "AspectSelector", "ResolutionPanel"]


class AspectSelector(QWidget):
    """Wide / Square / Portrait 텍스트 버튼 3개 (Req 10.3, 10.4)."""

    selected = Signal(object)  # Aspect

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current = Aspect.SQUARE

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[Aspect, QPushButton] = {}
        for aspect in Aspect:
            # 라벨은 Aspect.value 리터럴 — i18n을 거치지 않는다 (Req 10.3, 10.4).
            button = QPushButton(aspect.value, self)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.clicked.connect(partial(self._on_clicked, aspect))
            self._group.addButton(button)
            layout.addWidget(button)
            self._buttons[aspect] = button
        self._buttons[self._current].setChecked(True)

    # ── 조회 ────────────────────────────────────────────────────────────

    def current(self) -> Aspect:
        return self._current

    def button(self, aspect: Aspect) -> QPushButton:
        return self._buttons[aspect]

    def buttons(self) -> dict[Aspect, QPushButton]:
        return dict(self._buttons)

    # ── 조작 ────────────────────────────────────────────────────────────

    def set_available(self, aspects: Iterable[Aspect]) -> None:
        """해당 등급에 없는 Aspect 버튼을 비활성화한다 (Req 10.6)."""
        allowed = set(aspects)
        for aspect, button in self._buttons.items():
            button.setEnabled(aspect in allowed)

    def set_current(self, aspect: Aspect) -> None:
        """표시만 갱신한다 — `selected`를 emit하지 않는다."""
        self._current = aspect
        button = self._buttons[aspect]
        if not button.isChecked():
            button.setChecked(True)

    def _on_clicked(self, aspect: Aspect, _checked: bool = False) -> None:
        self.set_current(aspect)
        self.selected.emit(aspect)


class ResolutionPanel(QGroupBox):
    """항상 편집 가능한 해상도 영역 (Req 10.1, 10.2)."""

    changed = Signal()  # 너비/높이가 바뀔 때 (Req 10.14)

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._catalog: ResolutionCatalog | None = None
        self._source_locked = False
        self._source_size: tuple[int, int] | None = None
        #: i2i 잠금 직전의 t2i 크기 — 잠금이 풀리면 이 값으로 되돌린다.
        self._unlocked_size: tuple[int, int] | None = None
        # 프로그램이 값을 넣는 동안 사용자 조작 경로가 다시 발화하는 것을 막는다.
        self._syncing = False

        layout = QVBoxLayout(self)

        # 1행: 등급 콤보 + Aspect 버튼
        top_row = QHBoxLayout()
        self.group_label = QLabel()
        self.group_combo = QComboBox()
        self.group_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.aspect_selector = AspectSelector()
        top_row.addWidget(self.group_label)
        top_row.addWidget(self.group_combo, stretch=1)
        top_row.addWidget(self.aspect_selector)
        layout.addLayout(top_row)

        # 2행: 너비 × 높이
        size_row = QHBoxLayout()
        self.width_label = QLabel()
        self.width_spin = self._make_dimension_spin(DEFAULT_SIZE[0])
        self.times_label = QLabel("×")
        self.height_label = QLabel()
        self.height_spin = self._make_dimension_spin(DEFAULT_SIZE[1])
        size_row.addWidget(self.width_label)
        size_row.addWidget(self.width_spin)
        size_row.addWidget(self.times_label)
        size_row.addWidget(self.height_label)
        size_row.addWidget(self.height_spin)
        size_row.addStretch(1)
        layout.addLayout(size_row)

        # 3행: 경고 / 안내
        self.credit_warning_label = QLabel()
        self.credit_warning_label.setWordWrap(True)
        self.credit_warning_label.setStyleSheet("color: #c07a00;")
        self.credit_warning_label.setVisible(False)
        layout.addWidget(self.credit_warning_label)

        self.source_note_label = QLabel()
        self.source_note_label.setWordWrap(True)
        self.source_note_label.setStyleSheet("color: palette(mid);")
        self.source_note_label.setVisible(False)
        layout.addWidget(self.source_note_label)

        self._rebuild_group_combo()

        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.aspect_selector.selected.connect(self._on_aspect_selected)
        self.width_spin.valueChanged.connect(self._on_dimension_changed)
        self.height_spin.valueChanged.connect(self._on_dimension_changed)
        self.width_spin.editingFinished.connect(self._on_editing_finished)
        self.height_spin.editingFinished.connect(self._on_editing_finished)

        # 스크롤 중 값이 바뀌는 사고 방지 (기존 관습)
        self._wheel_guard = guard_wheel(self)

        self.retranslate()
        self._sync_aspect()
        self._refresh_indicators()

    @staticmethod
    def _make_dimension_spin(value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(MIN_DIMENSION, MAX_DIMENSION)
        spin.setSingleStep(DIMENSION_STEP)
        spin.setValue(value)
        return spin

    # ── 공개 API ────────────────────────────────────────────────────────

    def set_catalog(self, catalog: ResolutionCatalog) -> None:
        """등급 콤보를 다시 채운다. 현재 크기가 목록에 있으면 유지, 없으면 기본값 (Req 5.9, 5.10)."""
        self._catalog = catalog
        self._rebuild_group_combo()
        width, height = self.size()
        if catalog.groups() and not catalog.contains(width, height):
            width, height = catalog.default().size
        self._apply_size(width, height)

    def size(self) -> tuple[int, int]:
        """항상 스냅된 크기 (Req 10.12의 2차 방어선)."""
        return snap_size(self.width_spin.value(), self.height_spin.value())

    def set_size(self, width: int, height: int) -> None:
        self._apply_size(width, height)

    def set_source_locked(self, locked: bool, source_size: tuple[int, int] | None = None) -> None:
        """i2i / 인페인팅 잠금 (Req 10.13).

        생성 크기는 원본 이미지 크기를 따르므로 입력란·Aspect·등급 콤보를 모두 비활성화한다.
        등급 콤보까지 막는 이유: 등급을 바꾸면 무시될 크기가 입력란에 써지는 셈이라 오해를 부른다.

        잠금이 풀리면 잠기기 전 크기로 되돌린다 — 원본 이미지를 치웠을 때 t2i 크기가 원본
        크기에 물들어 있으면 안 된다.
        """
        was_locked = self._source_locked
        self._source_locked = bool(locked)
        self._source_size = source_size if self._source_locked else None
        if self._source_locked and not was_locked:
            # 잠기기 전 t2i 크기를 기억한다 — 원본을 치우면 사용자가 고르던 크기로 돌아간다.
            self._unlocked_size = (self.width_spin.value(), self.height_spin.value())
        if self._source_locked and source_size is not None:
            # 원본 크기는 스냅하지 않고 그대로 보여 준다 (실제 생성 크기와 어긋나면 안 된다).
            self._apply_size(source_size[0], source_size[1], snap=False)
        elif not self._source_locked and was_locked and self._unlocked_size is not None:
            restored, self._unlocked_size = self._unlocked_size, None
            self._apply_size(restored[0], restored[1], snap=False)
        for widget in (self.width_spin, self.height_spin, self.group_combo, self.aspect_selector):
            widget.setEnabled(not self._source_locked)
        self._refresh_indicators()

    def is_source_locked(self) -> bool:
        return self._source_locked

    def aspect_random_choices(self) -> tuple[tuple[int, int], ...]:
        """현재 등급에서 사용 가능한 각 Aspect의 대표 해상도 (랜덤 해상도 기능용).

        "직접 입력" 상태(`current_group()`이 None)면 카탈로그 전체에서 Aspect별 첫 해상도를 쓴다.
        """
        if self._catalog is None:
            return ()
        group = self.current_group()
        sizes: list[tuple[int, int]] = []
        for aspect in self._available_aspects():
            item = (
                self._catalog.first_of_aspect(group, aspect)
                if group is not None
                else self._first_of_aspect_any(aspect)
            )
            if item is not None:
                sizes.append(item.size)
        return tuple(sizes)

    def current_group(self) -> ResolutionGroup | None:
        """콤보에서 선택된 등급. "직접 입력"이면 None.

        userData는 문자열로 담는다 — PySide는 `str` 파생 Enum을 QVariant에 넣을 때
        평범한 문자열로 바꿔 버리므로, 열거형 인스턴스를 그대로 돌려받을 수 없다.
        """
        data = self.group_combo.currentData()
        if not isinstance(data, str) or data == DIRECT_ENTRY:
            return None
        try:
            return ResolutionGroup(data)
        except ValueError:
            return None

    def retranslate(self) -> None:
        """Aspect 버튼 라벨은 건드리지 않는다 (Req 10.4)."""
        tr = self._i18n.get_text
        self.setTitle(tr("resolution.title"))
        self.group_label.setText(tr("resolution.group"))
        self.width_label.setText(tr("resolution.width"))
        self.height_label.setText(tr("resolution.height"))
        for index in range(self.group_combo.count()):
            data = self.group_combo.itemData(index)
            self.group_combo.setItemText(index, self._group_text(data))
        self._refresh_indicators()

    # ── 내부: 콤보 ──────────────────────────────────────────────────────

    def _group_text(self, data: object) -> str:
        tr = self._i18n.get_text
        if data == DIRECT_ENTRY:
            return tr("resolution.direct_entry")
        return tr(f"resolution.group_{str(data).lower()}")

    def _rebuild_group_combo(self) -> None:
        previous = self._syncing
        self._syncing = True
        try:
            self.group_combo.clear()
            groups = self._catalog.groups() if self._catalog is not None else ()
            for group in groups:
                self.group_combo.addItem(self._group_text(group.value), userData=group.value)
            self.group_combo.addItem(self._group_text(DIRECT_ENTRY), userData=DIRECT_ENTRY)
        finally:
            self._syncing = previous

    def _index_of_data(self, data: object) -> int:
        """QComboBox.findData는 파이썬 객체 userData를 값 비교하지 못하므로 직접 순회한다."""
        for index in range(self.group_combo.count()):
            if self.group_combo.itemData(index) == data:
                return index
        return -1

    def _sync_group_combo(self) -> None:
        """현재 크기를 담고 있는 등급을 표시. 목록에 없으면 "직접 입력" (Req 10.10)."""
        width, height = self.width_spin.value(), self.height_spin.value()
        group = self._catalog.group_of(width, height) if self._catalog is not None else None
        index = self._index_of_data(group.value if group is not None else DIRECT_ENTRY)
        if index < 0:
            return
        previous = self._syncing
        self._syncing = True
        try:
            self.group_combo.setCurrentIndex(index)
        finally:
            self._syncing = previous

    # ── 내부: Aspect ────────────────────────────────────────────────────

    def _available_aspects(self) -> frozenset[Aspect]:
        """현재 등급에 존재하는 Aspect 집합. "직접 입력"이면 카탈로그 전체 (Req 10.6)."""
        if self._catalog is None:
            return frozenset(Aspect)
        group = self.current_group()
        if group is not None:
            return self._catalog.aspects(group)
        return frozenset(item.aspect for item in self._catalog.resolutions())

    def _sync_aspect(self) -> None:
        self.aspect_selector.set_available(self._available_aspects())
        self.aspect_selector.set_current(classify_aspect(self.width_spin.value(), self.height_spin.value()))

    def _first_of_aspect_any(self, aspect: Aspect) -> Resolution | None:
        """직접 입력 상태에서 Aspect 버튼을 눌렀을 때의 폴백 — 그 Aspect를 가진 첫 해상도."""
        if self._catalog is None:
            return None
        for item in self._catalog.resolutions():
            if item.aspect is aspect:
                return item
        return None

    # ── 내부: 크기 적용 ─────────────────────────────────────────────────

    def _apply_size(
        self,
        width: int,
        height: int,
        *,
        snap: bool = True,
        sync_group: bool = True,
    ) -> None:
        """프로그램 경로로 크기를 넣는다. 값이 실제로 바뀌면 `changed`를 emit한다."""
        if snap:
            width, height = snap_size(width, height)
        before = (self.width_spin.value(), self.height_spin.value())
        previous = self._syncing
        self._syncing = True
        try:
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)
        finally:
            self._syncing = previous
        if sync_group:
            self._sync_group_combo()
        self._sync_aspect()
        self._refresh_indicators()
        if before != (self.width_spin.value(), self.height_spin.value()):
            self.changed.emit()

    def _refresh_indicators(self) -> None:
        tr = self._i18n.get_text
        width, height = self.width_spin.value(), self.height_spin.value()
        over = exceeds_free_pixels(width, height)
        self.credit_warning_label.setText(tr("resolution.credit_warning") if over else "")
        self.credit_warning_label.setVisible(over)  # Req 10.11
        if self._source_locked:
            source = self._source_size or (width, height)
            self.source_note_label.setText(tr("resolution.source_locked", source[0], source[1]))
        else:
            self.source_note_label.setText("")
        self.source_note_label.setVisible(self._source_locked)  # Req 10.13

    # ── 사용자 조작 ─────────────────────────────────────────────────────

    def _on_group_changed(self, _index: int) -> None:
        """현재 Aspect를 유지하고 새 등급의 첫 해상도를 적용 (Req 10.7, 10.8)."""
        if self._syncing or self._catalog is None:
            return
        group = self.current_group()
        if group is None:  # "직접 입력"을 직접 골랐다 — 현재 크기를 그대로 둔다.
            self._sync_aspect()
            return
        aspect = self.aspect_selector.current()
        target = self._catalog.first_of_aspect(group, aspect) or self._catalog.first_of_group(group)
        if target is None:
            return
        # 사용자가 고른 등급 표시를 유지한다 (등급 간 중복 크기가 콤보를 되돌리지 않도록).
        self._apply_size(target.width, target.height, sync_group=False)

    def _on_aspect_selected(self, aspect: Aspect) -> None:
        """현재 등급에서 그 Aspect의 첫 해상도를 적용 (Req 10.5)."""
        if self._syncing or self._catalog is None:
            return
        group = self.current_group()
        if group is None:
            target = self._first_of_aspect_any(aspect)
            sync_group = True
        else:
            target = self._catalog.first_of_aspect(group, aspect)
            sync_group = False
        if target is None:  # 버튼이 이미 비활성이어야 하는 상태
            return
        self._apply_size(target.width, target.height, sync_group=sync_group)

    def _on_dimension_changed(self, _value: int) -> None:
        """직접 편집: 값은 그대로 두고 Aspect 버튼과 등급 표시만 갱신 (Req 10.9, 10.10, 10.14)."""
        if self._syncing:
            return
        self._sync_group_combo()
        self._sync_aspect()
        self._refresh_indicators()
        self.changed.emit()

    def _on_editing_finished(self) -> None:
        """편집 확정 시점에만 스냅한다 (Req 10.12).

        `valueChanged`마다 보정하면 `1` → `10` → `100`을 타이핑하는 중간 상태를 계속
        되돌려 입력이 불가능해진다.
        """
        if self._syncing:
            return
        width, height = snap_size(self.width_spin.value(), self.height_spin.value())
        if (width, height) != (self.width_spin.value(), self.height_spin.value()):
            self._apply_size(width, height)
