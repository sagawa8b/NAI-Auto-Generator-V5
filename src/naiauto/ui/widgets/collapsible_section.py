"""접이식 설정 섹션 — 헤더 + 요약 한 줄 + 본문.

자주 바꾸지 않는 설정(AI 설정 / 폴더)을 접어 두고, 접힌 동안에는 현재 값을 담은
요약 한 줄만 보여 준다. 본문 위젯은 `setVisible(False)`로 숨기기만 하므로 값은 그대로
살아 있고, 따라서 접힘 상태가 `GenerationJob` 스냅숏에 영향을 주지 않는다 (Req 11.10).

요약 문자열 조합은 위젯 밖의 순수 함수(`compose_ai_summary`)로 두어 Qt 없이도 검증할 수 있다.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from ...core.i18n.manager import I18nManager

#: 접힘/펼침 상태를 접근성 이름에 붙일 때 쓰는 i18n 키.
EXPANDED_STATE_KEY = "ui.section_expanded"
COLLAPSED_STATE_KEY = "ui.section_collapsed"
#: 접근성 이름의 "제목 — 상태" 구분자.
ACCESSIBLE_NAME_SEPARATOR = " — "


class SummaryStrip(QLabel):
    """접힌 섹션의 읽기 전용 요약 한 줄 (Req 11.9)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(False)
        # 마우스 선택(복사)만 허용한다 — 편집도, 포커스도 받지 않는다.
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("color: palette(mid);")


class SectionHeader(QToolButton):
    """접힘/펼침 토글 헤더 — 키보드 포커스와 접근성 이름을 갖는다 (Req 11.5, 11.11, 11.12)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setArrowType(Qt.ArrowType.RightArrow)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt 콜백)
        # Space는 QToolButton 기본 동작이 이미 클릭으로 처리한다. Enter/Return은 하지 않는다.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)


class CollapsibleSection(QWidget):
    """헤더 + 요약 한 줄 + 본문으로 이루어진 접이식 섹션."""

    toggled = Signal(bool)  # True = 펼침

    def __init__(
        self,
        i18n: I18nManager,
        title_key: str,
        *,
        summary_provider: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._title_key = title_key
        self._summary_provider = summary_provider
        self._content: QWidget | None = None
        self._expanded = False
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.header = SectionHeader(self)
        self.header.toggled.connect(self._on_header_toggled)
        layout.addWidget(self.header)

        self.summary = SummaryStrip(self)
        layout.addWidget(self.summary)

        self._layout = layout
        self.retranslate()
        self._apply_state()

    # ── 본문 ─────────────────────────────────────────────

    def set_content(self, widget: QWidget) -> None:
        """본문 위젯을 붙인다 (이전 본문은 레이아웃에서 떼어 낸다)."""
        if self._content is not None:
            self._layout.removeWidget(self._content)
            self._content.setParent(None)
        self._content = widget
        self._layout.addWidget(widget)
        self._apply_state()

    def content(self) -> QWidget | None:
        return self._content

    # ── 접힘 상태 ────────────────────────────────────────

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """접힘 상태를 정하고 `toggled`를 발신한다."""
        expanded = bool(expanded)
        if expanded == self._expanded:
            # 헤더 체크 상태가 어긋나 있을 수 있으니 표시만 맞춰 둔다.
            self._apply_state()
            return
        self._expanded = expanded
        self._apply_state()
        self.toggled.emit(expanded)

    def _on_header_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self.set_expanded(checked)

    def _apply_state(self) -> None:
        expanded = self._expanded
        self._syncing = True
        try:
            self.header.setChecked(expanded)
        finally:
            self._syncing = False
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)  # Req 11.12
        if self._content is not None:
            self._content.setVisible(expanded)  # Req 11.3, 11.4
        self.summary.setVisible(not expanded)
        self._update_accessible_name()

    def _update_accessible_name(self) -> None:
        tr = self._i18n.get_text
        state_key = EXPANDED_STATE_KEY if self._expanded else COLLAPSED_STATE_KEY
        name = f"{tr(self._title_key)}{ACCESSIBLE_NAME_SEPARATOR}{tr(state_key)}"
        self.header.setAccessibleName(name)  # Req 11.11
        self.header.setAccessibleDescription(name)

    # ── 요약 ─────────────────────────────────────────────

    def set_summary_provider(self, provider: Callable[[], str] | None) -> None:
        self._summary_provider = provider
        self.refresh_summary()

    def refresh_summary(self) -> None:
        """접힘 여부와 무관하게 요약 문자열을 다시 만든다 (Req 11.8)."""
        if self._summary_provider is None:
            return
        self.summary.setText(self._summary_provider())

    # ── i18n ─────────────────────────────────────────────

    def retranslate(self) -> None:
        self.header.setText(self._i18n.get_text(self._title_key))
        self._update_accessible_name()
        self.refresh_summary()


def compose_ai_summary(*, steps: int, cfg_scale: float, seed_label: str, sampler: str, template: str) -> str:
    """`AI 설정` 요약 한 줄을 만든다 (Req 11.6).

    `template`은 `tr("ui.summary_ai")` 결과로, 예: "스텝 {0} · PG {1} · 시드 {2} · {3}".
    Prompt Guidance는 `:g`로 포맷해 5.0이 "5"로, 5.5는 "5.5"로 보이게 한다.
    """
    return template.format(steps, f"{cfg_scale:g}", seed_label, sampler)


__all__ = [
    "ACCESSIBLE_NAME_SEPARATOR",
    "COLLAPSED_STATE_KEY",
    "EXPANDED_STATE_KEY",
    "CollapsibleSection",
    "SectionHeader",
    "SummaryStrip",
    "compose_ai_summary",
]
