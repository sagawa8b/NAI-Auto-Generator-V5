"""태그 자동완성 드롭다운 위젯.

프롬프트 QPlainTextEdit에 부착되어 textChanged 시그널을 감시하고,
현재 토큰(마지막 쉼표 이후 또는 필드 시작부터 커서 위치까지)을 추출하여
TagCompleter.suggest()를 호출한 뒤 결과를 팝업 리스트로 표시한다.

선택 시 부분 토큰을 선택된 태그 + ", "로 교체하고 커서를 구분자 뒤에 위치시킨다.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPlainTextEdit

from ..core.tag_completer import TagCompleter, TagEntry, strip_weight_prefix


class TagCompleterDropdown:
    """QCompleter-style popup attached to a QPlainTextEdit for tag suggestions.

    Parameters
    ----------
    text_edit : QPlainTextEdit
        The prompt input widget to attach to.
    completer : TagCompleter
        The core tag completer engine (must already be loaded).
    """

    def __init__(self, text_edit: QPlainTextEdit, completer: TagCompleter) -> None:
        self._text_edit = text_edit
        self._completer = completer

        # Popup list widget
        self._popup = QListWidget()
        self._popup.setWindowFlags(Qt.WindowType.ToolTip)
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._popup.setMaximumHeight(200)
        self._popup.setMinimumWidth(200)

        # Connect signals
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._popup.itemClicked.connect(self._on_item_selected)
        self._popup.itemActivated.connect(self._on_item_selected)

        # Install event filter for keyboard handling in text edit
        self._key_filter = self._make_key_filter()
        self._text_edit.installEventFilter(self._key_filter)

    def _make_key_filter(self):
        """텍스트 편집기에 붙일 이벤트 필터 객체를 만든다.

        `TagCompleterDropdown` 자체는 `QObject`가 아니라서 스스로 이벤트 필터가 될 수 없다
        (Qt 없이 이 모듈을 임포트하는 core 테스트가 있어 클래스 정의 시점에 `QObject`를
        요구할 수 없다). 필터 클래스를 함수 안에서 정의하면 임포트는 Qt 없이도 되고,
        실제 부착 시점에만 Qt가 필요하다.
        """
        from PySide6.QtCore import QObject

        owner = self

        class _KeyFilter(QObject):
            def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 콜백 이름)
                return owner.eventFilter(obj, event)

        return _KeyFilter(self._text_edit)

    # ── Public API ────────────────────────────────────────

    def detach(self) -> None:
        """Disconnect signals and clean up the popup."""
        try:
            self._text_edit.textChanged.disconnect(self._on_text_changed)
        except RuntimeError:
            pass
        self._text_edit.removeEventFilter(self._key_filter)
        self._key_filter.deleteLater()
        self._popup.hide()
        self._popup.deleteLater()

    # ── Token extraction ──────────────────────────────────

    @staticmethod
    def extract_current_token(text: str, cursor_pos: int) -> tuple[str, int]:
        """Extract the current token being typed and its start position.

        The token is the text after the last comma separator (or start of string)
        up to the cursor position, stripped of leading whitespace.

        Returns
        -------
        tuple[str, int]
            (token_text, token_start_position_in_full_text)
        """
        # Get text up to cursor
        text_to_cursor = text[:cursor_pos]

        # Find the last comma before cursor
        last_comma = text_to_cursor.rfind(",")
        if last_comma == -1:
            # No comma found — token starts at beginning
            token_start = 0
        else:
            # Token starts after the comma
            token_start = last_comma + 1

        # Get raw token (may have leading whitespace)
        raw_token = text_to_cursor[token_start:]

        # Strip leading whitespace but track how many chars were stripped
        stripped = raw_token.lstrip()
        whitespace_len = len(raw_token) - len(stripped)

        return stripped, token_start + whitespace_len

    # ── Signal handlers ───────────────────────────────────

    def _on_text_changed(self) -> None:
        """Handle text changes: extract token and show/hide popup."""
        if not self._completer.is_enabled:
            self._popup.hide()
            return

        cursor = self._text_edit.textCursor()
        cursor_pos = cursor.position()
        text = self._text_edit.toPlainText()

        token, _ = self.extract_current_token(text, cursor_pos)
        _weight, keyword = strip_weight_prefix(token)  # "1.5::blue" → "blue"로 검색

        if len(keyword) < 2:
            self._popup.hide()
            return

        suggestions = self._completer.suggest(keyword)
        if not suggestions:
            self._popup.hide()
            return

        self._show_suggestions(suggestions)

    def _show_suggestions(self, suggestions: list[TagEntry]) -> None:
        """Populate and position the popup with suggestions."""
        self._popup.clear()
        for entry in suggestions:
            item = QListWidgetItem(f"{entry.name}  ({entry.post_count:,})")
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self._popup.addItem(item)

        # Position popup below the cursor
        cursor_rect = self._text_edit.cursorRect()
        global_pos = self._text_edit.mapToGlobal(QPoint(cursor_rect.x(), cursor_rect.bottom()))
        self._popup.move(global_pos)
        self._popup.setCurrentRow(0)
        self._popup.show()

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        """Replace partial token with selected tag + ', '."""
        tag_name = item.data(Qt.ItemDataRole.UserRole)
        if not tag_name:
            self._popup.hide()
            return

        self._apply_completion(tag_name)
        self._popup.hide()

    def _apply_completion(self, tag_name: str) -> None:
        """Replace the current partial token with tag_name + ', ' and reposition cursor.

        태그의 언더스코어는 공백으로 바꾸고(NAI 프롬프트 표기), 사용자가 적어 둔
        가중치 접두사("1.5::")는 그대로 살린다 — 둘 다 V4.5와 같은 동작이다.
        """
        cursor = self._text_edit.textCursor()
        cursor_pos = cursor.position()
        text = self._text_edit.toPlainText()

        token, token_start = self.extract_current_token(text, cursor_pos)
        weight_prefix, _keyword = strip_weight_prefix(token)

        # Build replacement: [가중치 접두사 +] tag + ", "
        replacement = weight_prefix + tag_name.replace("_", " ") + ", "

        # Block signals to avoid re-triggering textChanged during replacement
        self._text_edit.blockSignals(True)
        try:
            # Select from token start to current cursor position
            cursor.setPosition(token_start, QTextCursor.MoveMode.MoveAnchor)
            cursor.setPosition(cursor_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(replacement)
            # Cursor is now positioned after the inserted text
            self._text_edit.setTextCursor(cursor)
        finally:
            self._text_edit.blockSignals(False)

    # ── Event filter for keyboard interaction ─────────────

    def eventFilter(self, obj: object, event: object) -> bool:  # noqa: N802
        """Handle keyboard events when popup is visible."""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        if obj is not self._text_edit or not self._popup.isVisible():
            return False

        if not isinstance(event, QKeyEvent):
            return False

        if event.type() != QEvent.Type.KeyPress:
            return False

        key = event.key()

        if key == Qt.Key.Key_Escape:
            self._popup.hide()
            return True

        if key == Qt.Key.Key_Down:
            current = self._popup.currentRow()
            if current < self._popup.count() - 1:
                self._popup.setCurrentRow(current + 1)
            return True

        if key == Qt.Key.Key_Up:
            current = self._popup.currentRow()
            if current > 0:
                self._popup.setCurrentRow(current - 1)
            return True

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            current_item = self._popup.currentItem()
            if current_item:
                self._on_item_selected(current_item)
            return True

        return False


__all__ = ["TagCompleterDropdown"]
