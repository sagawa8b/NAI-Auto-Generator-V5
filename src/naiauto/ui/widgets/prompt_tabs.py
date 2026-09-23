"""프롬프트 / Undesired Content 전환 탭.

두 입력창을 세로로 쌓지 않고 탭으로 전환한다 (NovelAI 웹 UI, 구 V4.5 앱과 동일).
같은 공간에 더 큰 입력창을 두면서 좌측 패널 높이를 절반 가까이 줄인다.
메인 프롬프트와 캐릭터 슬롯이 같은 위젯을 쓴다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTabWidget, QToolButton, QWidget

from ...core.i18n.manager import I18nManager
from ...core.prompt_cleanup import clean_prompt
from .prompt_highlighter import PromptHighlighter

# 이미지 정보를 읽을 수 있는 확장자. 이 파일들은 텍스트로 붙이지 않고 시그널로 넘긴다.
_IMAGE_SUFFIXES = {".png", ".webp"}


class PromptTextEdit(QPlainTextEdit):
    """이미지 파일을 끌어다 놓으면 경로를 붙이지 않고 `image_dropped`를 낸다.

    기본 QPlainTextEdit는 파일 URL을 받으면 경로 문자열을 그대로 삽입한다 —
    프롬프트에 이미지를 던진 사용자가 원한 동작이 아니다 (V4에서는 이미지 정보
    창이 떴다). 이미지가 아닌 드롭(텍스트, 다른 확장자)은 원래대로 둔다.
    """

    image_dropped = Signal(str)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt 콜백 이름)
        if _dropped_image(event) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        # dragEnter에서 받아 놓고 여기서 거절하면 커서가 "금지"로 바뀌고 드롭이 죽는다
        if _dropped_image(event) is not None:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt 콜백 이름)
        path = _dropped_image(event)
        if path is not None:
            event.acceptProposedAction()
            self.image_dropped.emit(path)
            return
        super().dropEvent(event)


def _dropped_image(event) -> str | None:
    """드롭 이벤트에서 이미지 파일 경로 하나를 꺼낸다 (없으면 None)."""
    mime = event.mimeData()
    if not mime.hasUrls():
        return None
    for url in mime.urls():
        local = url.toLocalFile()
        if local and Path(local).suffix.lower() in _IMAGE_SUFFIXES:
            return local
    return None


class PromptTabs(QTabWidget):
    """프롬프트 탭 + 네거티브 탭. 네거티브 탭 제목에 글자 수를 표시한다."""

    #: 두 입력창 어느 쪽에 놓아도 같은 시그널로 모인다 (메인 창이 이미지 정보를 연다)
    image_dropped = Signal(str)

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

        self.prompt_edit = PromptTextEdit()
        self.negative_edit = PromptTextEdit()
        for edit in (self.prompt_edit, self.negative_edit):
            edit.image_dropped.connect(self.image_dropped)
        # 가중치·괄호 구문 강조 (V4.5 동작) — 메인·캐릭터 슬롯이 이 위젯을 공유한다
        self._highlighters = [
            PromptHighlighter(self.prompt_edit.document()),
            PromptHighlighter(self.negative_edit.document()),
        ]
        self.addTab(self.prompt_edit, "")
        self.addTab(self.negative_edit, "")
        self.negative_edit.textChanged.connect(self._refresh_negative_title)

        # `태그 정리` — 지금 보이는 탭의 가중치 문법·공백을 표준 표기로 다듬는다.
        # 탭바 모서리에 둬서 프롬프트·네거티브 어느 쪽이든 같은 버튼으로 정리한다.
        self.cleanup_button = QToolButton()
        self.cleanup_button.clicked.connect(self.cleanup_current)
        self.setCornerWidget(self.cleanup_button, Qt.Corner.TopRightCorner)

        self.retranslate()

    # ── 상태 ─────────────────────────────────────────────

    def texts(self) -> tuple[str, str]:
        return self.prompt_edit.toPlainText(), self.negative_edit.toPlainText()

    def prompt(self) -> str:
        """메인 프롬프트 텍스트 (네거티브가 아니라 항상 프롬프트 탭)."""
        return self.prompt_edit.toPlainText()

    def set_prompt(self, text: str) -> None:
        """메인 프롬프트 텍스트를 갈아 끼운다."""
        self.prompt_edit.setPlainText(text)

    def _current_edit(self) -> PromptTextEdit:
        """지금 보이는 탭의 입력창 (프롬프트 또는 네거티브)."""
        return self.negative_edit if self.currentIndex() == 1 else self.prompt_edit

    def cleanup_current(self) -> None:
        """지금 보이는 입력창의 텍스트를 `clean_prompt`로 다듬는다.

        바뀐 게 없으면 손대지 않는다 — 커서·실행취소 이력을 괜히 흔들지 않는다.
        """
        edit = self._current_edit()
        original = edit.toPlainText()
        cleaned = clean_prompt(original)
        if cleaned != original:
            # `setPlainText`는 실행취소 이력을 지운다 — 커서로 통째 갈아 끼워 Ctrl+Z 한 번에
            # 되돌릴 수 있게 한다.
            cursor = edit.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(cleaned)
            cursor.endEditBlock()

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
        self.cleanup_button.setText(tr("ui.tag_cleanup"))
        self.cleanup_button.setToolTip(tr("ui.tag_cleanup_tooltip"))
        self._refresh_negative_title()


__all__ = ["PromptTabs", "PromptTextEdit"]
