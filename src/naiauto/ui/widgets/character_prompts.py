"""캐릭터 프롬프트 위젯 — 다중 캐릭터 슬롯 + 자유 위치 지정.

V5도 캐릭터 프롬프트를 지원한다 (2026-08-21 캡처의 `characterPrompts` 확인).
좌표는 payload의 `center` 그대로인 0.0~1.0 값이고, 웹 UI처럼 생성 해상도 비율의
캔버스에서 마커를 끌어 자유롭게 지정한다 (구 5×5 그리드는 안내선으로만 남는다).
캐릭터가 1명이면 좌표는 의미가 없어 0.5/0.5로 고정된다 (클라이언트가 강제).
"AI 위치 선택"을 켜면 좌표를 보내지 않고(use_coords=False) AI가 배치를 정한다.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.api.models import CharacterCaption
from ...core.i18n.manager import I18nManager
from .collapsible_section import CollapsibleSection
from .flow_layout import FlowLayout
from .position_picker import CANVAS_HEIGHT, PositionPicker, clamp_coord
from .prompt_tabs import PromptTabs
from .resize_handle import ResizeHandle
from .wheel_guard import WheelGuard, guard_wheel

#: 위치 지정 캔버스 높이를 담는 QSettings 키 (접힘 상태는 settings.ui가 들고 있다).
POSITION_HEIGHT_KEY = "ui/position_picker_height"
#: 접힌 위치 패널 요약에서 캐릭터 사이를 잇는 구분자.
POSITION_SUMMARY_SEPARATOR = " · "
#: 접힌 캐릭터 슬롯 헤더에 남기는 프롬프트 미리보기 길이 (글자 수).
SLOT_PREVIEW_LENGTH = 40

X_LABELS = ("A", "B", "C", "D", "E")
Y_LABELS = ("1", "2", "3", "4", "5")
GRID_VALUES = (0.1, 0.3, 0.5, 0.7, 0.9)  # 안내선/셀 이름 계산용 (스냅은 하지 않는다)
DEFAULT_CENTER = 0.5


def slot_preview(prompt: str, limit: int = SLOT_PREVIEW_LENGTH) -> str:
    """접힌 슬롯 헤더에 남길 프롬프트 한 줄 (줄바꿈은 공백으로, 길면 말줄임)."""
    flat = " ".join(prompt.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def grid_cell_name(center_x: float, center_y: float) -> str:
    """가장 가까운 5×5 셀 이름 (예: 0.28/0.68 → "B4"). 표시 전용."""
    x = min(range(len(GRID_VALUES)), key=lambda i: abs(GRID_VALUES[i] - center_x))
    y = min(range(len(GRID_VALUES)), key=lambda i: abs(GRID_VALUES[i] - center_y))
    return f"{X_LABELS[x]}{Y_LABELS[y]}"


class CharacterSlot(QFrame):
    """캐릭터 1명. 프롬프트/네거티브 탭 + 위치 + 활성화/삭제."""

    remove_requested = Signal(object)
    changed = Signal()
    height_changed = Signal()

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._center = (DEFAULT_CENTER, DEFAULT_CENTER)
        self._positioned = False  # 사용자가 직접 옮겼는가 (자동 배치 대상 판별)
        self._position_active = False
        self._collapsed = False
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        # 접기 화살표 — 슬롯이 서너 개만 돼도 세로가 금방 찬다. 접으면 헤더 한 줄만 남는다.
        self.collapse_button = QToolButton()
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setArrowType(Qt.ArrowType.DownArrow)
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(True)
        self.title_label = QLabel()
        # 접혔을 때만 보이는 프롬프트 미리보기. Ignored로 두어 이 글자 수가 슬롯의
        # 최소 폭이 되지 않게 한다 (좁힌 패널에서 잘려도 되는 글이다).
        self.preview_label = QLabel()
        self.preview_label.setStyleSheet("color: palette(mid);")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_label.setVisible(False)
        self.position_label = QLabel()
        self.position_value = QLabel()
        self.position_value.setStyleSheet("color: palette(mid);")
        self.remove_button = QPushButton("✕")
        self.remove_button.setFixedWidth(30)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))

        header.addWidget(self.collapse_button)
        header.addWidget(self.enabled_check)
        header.addWidget(self.title_label)
        header.addWidget(self.preview_label, stretch=1)
        header.addStretch(1)
        header.addWidget(self.position_label)
        header.addWidget(self.position_value)
        header.addWidget(self.remove_button)
        layout.addLayout(header)

        self.tabs = PromptTabs(
            i18n,
            prompt_placeholder="ui.character_prompt",
            negative_placeholder="ui.negative_prompt_add",
        )
        layout.addWidget(self.tabs)

        self._resize_handle = ResizeHandle(
            target=self.tabs,
            settings=QSettings(),
            parent=self,
            settings_key="ui/character_slot_height",
            default_height=120,
        )
        layout.addWidget(self._resize_handle)
        self._resize_handle.height_persisted.connect(self.height_changed)

        # 다른 코드가 쓰는 이름은 그대로 유지한다 (탭 안의 같은 편집기)
        self.prompt_edit = self.tabs.prompt_edit
        self.uc_edit = self.tabs.negative_edit
        self.prompt_edit.textChanged.connect(self.changed)
        self.uc_edit.textChanged.connect(self.changed)
        self.prompt_edit.textChanged.connect(self._refresh_preview)
        self._refresh_position_text()
        self._resize_handle.restore_height()

    # ── 상태 ─────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self.enabled_check.isChecked()

    @property
    def center(self) -> tuple[float, float]:
        return self._center

    @property
    def collapsed(self) -> bool:
        """본문(프롬프트 탭)을 접어 헤더 한 줄만 남긴 상태인가."""
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """본문을 접거나 편다. 접어도 값은 그대로 살아 있다 (숨기기만 한다)."""
        self._collapsed = bool(collapsed)
        self.tabs.setVisible(not self._collapsed)
        self._resize_handle.setVisible(not self._collapsed)
        self.collapse_button.setArrowType(
            Qt.ArrowType.RightArrow if self._collapsed else Qt.ArrowType.DownArrow
        )
        self.preview_label.setVisible(self._collapsed)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if self._collapsed:
            self.preview_label.setText(slot_preview(self.prompt_edit.toPlainText()))

    @property
    def positioned(self) -> bool:
        """사용자(또는 메타데이터)가 위치를 정한 슬롯인가."""
        return self._positioned

    def set_center(self, center_x: float, center_y: float, *, by_user: bool = True) -> None:
        self._center = (clamp_coord(center_x), clamp_coord(center_y))
        if by_user:
            self._positioned = True
        self._refresh_position_text()

    def set_index(self, index: int) -> None:
        """1부터 시작하는 표시 번호 (삭제 후 재번호 매김에 사용)."""
        self._index = index
        self.title_label.setText(self._i18n.get_text("ui.character_n", index))

    def set_position_enabled(self, enabled: bool) -> None:
        """캐릭터 1명이거나 AI 위치 선택이면 좌표 대신 '자동'을 보여준다."""
        self._position_active = enabled
        self.position_label.setEnabled(enabled)
        self.position_value.setEnabled(enabled)
        self._refresh_position_text()

    def to_caption(self, use_coords: bool) -> CharacterCaption | None:
        """비활성화되었거나 프롬프트가 비면 None (요청에서 제외)."""
        prompt = self.prompt_edit.toPlainText().strip()
        if not self.is_enabled or not prompt:
            return None
        center_x, center_y = self._center if use_coords else (DEFAULT_CENTER, DEFAULT_CENTER)
        return CharacterCaption(
            prompt=prompt,
            uc=self.uc_edit.toPlainText().strip(),
            center_x=center_x,
            center_y=center_y,
        )

    def load_caption(self, caption: CharacterCaption) -> None:
        self.prompt_edit.setPlainText(caption.prompt)
        self.uc_edit.setPlainText(caption.uc)
        self.enabled_check.setChecked(True)
        # 중앙(0.5/0.5)은 "지정하지 않음"과 구분되지 않으므로 자동 배치 대상으로 남긴다
        centered = caption.center_x == DEFAULT_CENTER and caption.center_y == DEFAULT_CENTER
        self.set_center(caption.center_x, caption.center_y, by_user=not centered)

    def _refresh_position_text(self) -> None:
        if not self._position_active:
            self.position_value.setText(self._i18n.get_text("ui.position_auto"))
            return
        x, y = self._center
        self.position_value.setText(f"{grid_cell_name(x, y)} ({x:.2f}, {y:.2f})")

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.title_label.setText(tr("ui.character_n", getattr(self, "_index", 1)))
        self.position_label.setText(tr("ui.position"))
        self.tabs.retranslate()
        self.remove_button.setToolTip(tr("ui.clear_all"))
        self.collapse_button.setToolTip(tr("ui.toggle_slot"))


class CharacterPromptsWidget(QGroupBox):
    """캐릭터 슬롯 목록 + AI 위치 선택 토글 + 위치 캔버스."""

    #: 슬롯이 생기거나 사라질 때 — 메인 윈도우가 태그 자동완성을 붙이고 뗀다.
    slot_added = Signal(object)
    slot_removed = Signal(object)
    #: captions()/use_coords()가 바뀔 수 있는 모든 변화 — 연속 생성 중 다음 이미지에
    #: 반영하기 위해 메인 윈도우가 구독한다.
    prompts_changed = Signal()

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._slots: list[CharacterSlot] = []
        self._wheel_guard: WheelGuard | None = None

        layout = QVBoxLayout(self)

        # FlowLayout — 좌측 패널을 좁히면 체크박스와 버튼이 다음 줄로 넘어간다.
        controls = FlowLayout()
        self.ai_position_check = QCheckBox()
        self.ai_position_check.setChecked(False)
        self.ai_position_check.toggled.connect(self._refresh_position_enabled)
        self.ai_position_check.toggled.connect(self.prompts_changed)
        self.manual_position_check = QCheckBox()
        self.manual_position_check.setChecked(False)
        self.manual_position_check.toggled.connect(self._refresh_position_enabled)
        self.manual_position_check.toggled.connect(self.prompts_changed)
        self.add_button = QPushButton()
        self.add_button.clicked.connect(self.add_character)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self.clear_all)
        controls.addWidget(self.ai_position_check)
        controls.addWidget(self.manual_position_check)
        controls.addWidget(self.add_button)
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)

        # 위치 지정 캔버스는 접을 수 있다 — 좌표를 한 번 잡고 나면 200px 넘는 캔버스가
        # 캐릭터 슬롯을 화면 밖으로 밀어내기만 한다. 접으면 지금 좌표를 요약 한 줄로 보여 준다.
        self.position_picker = PositionPicker()
        self.position_picker.moved.connect(self._on_marker_moved)
        self.position_hint = QLabel()
        self.position_hint.setWordWrap(True)
        self.position_hint.setStyleSheet("color: palette(mid);")

        position_body = QWidget()
        body_layout = QVBoxLayout(position_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(2)
        body_layout.addWidget(self.position_hint)
        body_layout.addWidget(self.position_picker)
        # 캔버스 높이도 드래그로 조절한다 (마커를 정확히 놓으려면 넓은 편이 낫다).
        self._picker_handle = ResizeHandle(
            target=self.position_picker,
            settings=QSettings(),
            parent=position_body,
            settings_key=POSITION_HEIGHT_KEY,
            default_height=CANVAS_HEIGHT,
        )
        body_layout.addWidget(self._picker_handle)
        self._picker_handle.restore_height()

        self.position_section = CollapsibleSection(
            i18n, "ui.position_panel", summary_provider=self._compose_position_summary
        )
        self.position_section.set_content(position_body)
        self.position_section.set_expanded(True)
        layout.addWidget(self.position_section)

        self._slots_layout = QVBoxLayout()
        self._slots_layout.setSpacing(6)
        layout.addLayout(self._slots_layout)
        layout.addStretch(0)

        self._refresh_position_enabled()

    # ── 슬롯 관리 ────────────────────────────────────────

    @property
    def slots(self) -> list[CharacterSlot]:
        return list(self._slots)

    def share_wheel_guard(self, guard: WheelGuard) -> None:
        """나중에 만드는 슬롯에도 같은 휠 가드를 걸기 위해 넘겨받는다."""
        self._wheel_guard = guard

    def add_character(self) -> CharacterSlot:
        slot = CharacterSlot(self._i18n, self)
        if self._wheel_guard is not None:
            guard_wheel(slot, self._wheel_guard)
        slot.remove_requested.connect(self.remove_character)
        slot.enabled_check.toggled.connect(self._refresh_position_enabled)
        slot.enabled_check.toggled.connect(self.prompts_changed)
        slot.changed.connect(self._refresh_position_enabled)
        slot.changed.connect(self.prompts_changed)
        slot.height_changed.connect(self._sync_slot_heights)
        self._slots.append(slot)
        self._slots_layout.addWidget(slot)
        slot.retranslate()
        self._renumber()
        self.slot_added.emit(slot)
        self.prompts_changed.emit()
        return slot

    def remove_character(self, slot: CharacterSlot) -> None:
        if slot not in self._slots:
            return
        self.slot_removed.emit(slot)  # 위젯이 살아 있는 동안 부착물을 떼어 낼 기회를 준다
        self._slots.remove(slot)
        self._slots_layout.removeWidget(slot)
        slot.setParent(None)
        slot.deleteLater()
        self._renumber()
        self.prompts_changed.emit()

    def clear_all(self) -> None:
        for slot in list(self._slots):
            self.remove_character(slot)

    def _renumber(self) -> None:
        for i, slot in enumerate(self._slots, start=1):
            slot.set_index(i)
        self._refresh_position_enabled()

    def _sync_slot_heights(self) -> None:
        """모든 슬롯의 PromptTabs 높이를 QSettings에서 읽은 값으로 동기화."""
        settings = QSettings()
        raw = settings.value("ui/character_slot_height")
        if raw is None:
            return
        try:
            height = int(raw)
        except (TypeError, ValueError):
            return
        if not (120 <= height <= 600):
            return
        for slot in self._slots:
            slot.tabs.setFixedHeight(height)

    # ── 위치 ─────────────────────────────────────────────

    def set_aspect(self, width: int, height: int) -> None:
        """위치 캔버스를 현재 생성 해상도 비율로 맞춘다."""
        self.position_picker.set_aspect(width, height)

    def _marker_slots(self) -> list[CharacterSlot]:
        """캔버스에 마커로 올릴 슬롯 = 체크된 슬롯.

        프롬프트를 아직 안 쓴 슬롯도 포함한다 — 캐릭터를 추가하자마자 자리를
        먼저 잡는 순서가 자연스럽고, 프롬프트가 비면 어차피 요청에서 빠진다.
        """
        return [s for s in self._slots if s.is_enabled]

    def _on_marker_moved(self, index: int, center_x: float, center_y: float) -> None:
        placed = self._marker_slots()
        if 0 <= index < len(placed):
            placed[index].set_center(center_x, center_y)
            self.position_section.refresh_summary()  # 접었을 때 보일 요약을 최신 좌표로

    def _auto_arrange(self, placed: list[CharacterSlot]) -> None:
        """직접 옮긴 적 없는 슬롯을 가로로 고르게 벌린다.

        전부 0.5/0.5로 시작하면 마커가 겹쳐 보여서 캐릭터를 추가해도 화면이
        그대로인 것처럼 보인다. 웹 UI처럼 새 캐릭터가 눈에 띄는 자리에 놓이게 한다.
        """
        count = len(placed)
        if count < 2:
            return
        for i, slot in enumerate(placed):
            if not slot.positioned:
                slot.set_center((i + 0.5) / count, DEFAULT_CENTER, by_user=False)

    def _refresh_position_enabled(self) -> None:
        """좌표 지정 가능 조건: AI 위치 선택 꺼짐 + 체크된 캐릭터 2명 이상 (또는 수동 위치 토글 ON + 1명)."""
        placed = self._marker_slots()
        enabled = self.use_coords() and (
            len(placed) >= 2 or (len(placed) == 1 and self.manual_position_check.isChecked())
        )
        if enabled:
            self._auto_arrange(placed)
        for slot in self._slots:
            slot.set_position_enabled(enabled and slot.is_enabled)
        # 좌표를 못 쓰는 상황에서는 접기 헤더까지 통째로 감춘다 — 접어 둔 상태여도
        # 캔버스가 살아 있는 것처럼 보이면 안 된다.
        self.position_section.setVisible(enabled)
        # 숨겨져 있어도 마커 목록은 항상 실제 슬롯과 맞춰 둔다 (삭제된 슬롯이 남지 않도록).
        # 라벨은 슬롯 번호 그대로 — 캔버스의 "2"와 "캐릭터 2"가 어긋나지 않게.
        self.position_picker.set_points([(str(self._slots.index(slot) + 1), *slot.center) for slot in placed])
        self.position_section.refresh_summary()

    # ── 위치 패널 접기 ───────────────────────────────────

    def position_panel_expanded(self) -> bool:
        """위치 지정 패널이 펼쳐져 있는가."""
        return self.position_section.is_expanded()

    def set_position_panel_expanded(self, expanded: bool) -> None:
        """설정 복원용 — False면 캔버스를 접고 요약 한 줄만 남긴다."""
        self.position_section.refresh_summary()
        self.position_section.set_expanded(expanded)

    def _compose_position_summary(self) -> str:
        """접힌 위치 패널에 보일 한 줄 (예: "1 C3 · 2 D3").

        캔버스의 마커가 아니라 슬롯이 들고 있는 좌표를 읽는다 — 마커 목록은
        `_refresh_position_enabled()`가 돌 때만 갱신되므로 한 박자 늦을 수 있다.
        """
        return POSITION_SUMMARY_SEPARATOR.join(
            f"{self._slots.index(slot) + 1} {grid_cell_name(*slot.center)}" for slot in self._marker_slots()
        )

    # ── 요청 데이터 ──────────────────────────────────────

    def use_coords(self) -> bool:
        return not self.ai_position_check.isChecked()

    def set_use_coords(self, use_coords: bool) -> None:
        """설정 복원용 — False면 'AI 위치 선택'이 켜진다."""
        self.ai_position_check.setChecked(not use_coords)
        self._refresh_position_enabled()

    def manual_position_override(self) -> bool:
        """수동 위치 지정 토글 상태."""
        return self.manual_position_check.isChecked()

    def set_manual_position_override(self, enabled: bool) -> None:
        """설정 복원용 — True면 수동 위치 지정이 켜진다."""
        self.manual_position_check.setChecked(enabled)
        self._refresh_position_enabled()

    def captions(self) -> tuple[CharacterCaption, ...]:
        use_coords = self.use_coords()
        enabled_slots = [s for s in self._slots if s.is_enabled and s.prompt_edit.toPlainText().strip()]
        # 수동 위치 토글 ON + 1명이면 좌표를 보낸다 (기존: 1명이면 항상 0.5/0.5)
        if self.manual_position_check.isChecked() and len(enabled_slots) == 1:
            use_coords = True
        result = [s.to_caption(use_coords) for s in self._slots]
        return tuple(c for c in result if c is not None)

    def load_captions(self, captions: tuple[CharacterCaption, ...]) -> None:
        """메타데이터/설정에서 캐릭터 목록을 복원한다."""
        self.clear_all()
        for caption in captions:
            self.add_character().load_caption(caption)
        self._refresh_position_enabled()

    # ── i18n ─────────────────────────────────────────────

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setTitle(tr("ui.character_prompts"))
        # 설명은 자리를 차지하지 않게 툴팁으로만 둔다
        self.setToolTip(tr("ui.character_prompt_info"))
        self.ai_position_check.setText(tr("ui.ai_position"))
        self.manual_position_check.setText(tr("ui.manual_position"))
        self.add_button.setText(tr("ui.add_character"))
        self.clear_button.setText(tr("ui.clear_all"))
        self.position_hint.setText(tr("ui.position_hint"))
        self.position_section.retranslate()
        for slot in self._slots:
            slot.retranslate()


__all__ = [
    "GRID_VALUES",
    "POSITION_HEIGHT_KEY",
    "POSITION_SUMMARY_SEPARATOR",
    "SLOT_PREVIEW_LENGTH",
    "X_LABELS",
    "Y_LABELS",
    "CharacterPromptsWidget",
    "CharacterSlot",
    "grid_cell_name",
    "slot_preview",
]
