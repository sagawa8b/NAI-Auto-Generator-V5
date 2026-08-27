"""프롬프트 / Undesired Content 전환 탭.

두 입력창을 세로로 쌓지 않고 탭으로 전환한다 (NovelAI 웹 UI, 구 V4.5 앱과 동일).
같은 공간에 더 큰 입력창을 두면서 좌측 패널 높이를 절반 가까이 줄인다.
메인 프롬프트와 캐릭터 슬롯이 같은 위젯을 쓴다.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPlainTextEdit, QTabWidget, QWidget

from ...core.i18n.manager import I18nManager
from .prompt_highlighter import PromptHighlighter


class PromptTabs(QTabWidget):
    """프롬프트 탭 + 네거티브 탭. 네거티브 탭 제목에 글자 수를 표시한다."""

    def __init__(
        self,
        i18n: I18nManager,
        prompt_placeholder: str = "ui.prompt_placeholder",
        negative_placeholder: str = "ui.negative_prompt_placeholder",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._prompt_placeholder = prompt_placeholder
        self._negative_placeholder = negative_placeholder

        self.prompt_edit = QPlainTextEdit()
        self.negative_edit = QPlainTextEdit()
        # 가중치·괄호 구문 강조 (V4.5 동작) — 메인·캐릭터 슬롯이 이 위젯을 공유한다
        self._highlighters = [
            PromptHighlighter(self.prompt_edit.document()),
            PromptHighlighter(self.negative_edit.document()),
        ]
        self.addTab(self.prompt_edit, "")
        self.addTab(self.negative_edit, "")
        self.negative_edit.textChanged.connect(self._refresh_negative_title)

        self.retranslate()

    # ── 상태 ─────────────────────────────────────────────

    def texts(self) -> tuple[str, str]:
        return self.prompt_edit.toPlainText(), self.negative_edit.toPlainText()

    def set_emphasis_colors(self, high_color: QColor | None, low_color: QColor | None) -> None:
        """가중치 강조(>1.0)/약화(<1.0) 색을 두 편집기 모두에 적용한다. None이면 기본 고정색."""
        for highlighter in self._highlighters:
            highlighter.set_colors(high_color, low_color)

    def show_negative(self) -> None:
        """네거티브 탭으로 전환 (설정 재사용 등으로 값이 바뀌었을 때 알리기 위해)."""
        self.setCurrentIndex(1)

    # ── 표시 ─────────────────────────────────────────────

    def _refresh_negative_title(self) -> None:
        label = self._i18n.get_text("ui.undesired_content")
        length = len(self.negative_edit.toPlainText())
        self.setTabText(1, f"{label} ({length})" if length else label)

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setTabText(0, tr("ui.prompt"))
        self.prompt_edit.setPlaceholderText(tr(self._prompt_placeholder))
        self.negative_edit.setPlaceholderText(tr(self._negative_placeholder))
        self._refresh_negative_title()


__all__ = ["PromptTabs"]
