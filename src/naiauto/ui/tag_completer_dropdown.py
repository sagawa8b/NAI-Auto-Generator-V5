"""프롬프트 자동완성 드롭다운 위젯 (태그 + 와일드카드).

프롬프트 QPlainTextEdit에 부착되어 textChanged 시그널을 감시하고, 커서 앞에서 입력 중인
토큰을 `core.prompt_token.token_at_cursor()`로 읽는다 (구분자와 와일드카드 여는 기호를
가려내는 규칙은 그쪽에 있다).

토큰의 종류에 따라 목록이 갈린다:

- `__` / `__=` / `##`로 여는 토큰 → **와일드카드 이름** (`WildcardCatalog`). 여는 기호만
  쳐도 폴더 안 목록이 그대로 뜨고, 고르면 닫는 기호까지 붙는다 (`__folder/name__`).
- 그 밖 → **태그** (`TagCompleter.suggest()`). 구간 전체로 못 찾으면 **마지막 낱말**로
  한 번 더 찾는다 — 쉼표 없이 이어 쓴 `1girl, solo masterpi`에서도 뜨게 하려는 것이다
  (`blue eyes`처럼 낱말이 여럿인 태그를 위해 공백은 경계로 두지 않는다).

선택 시 부분 토큰을 완성된 값 + ", "로 교체하고 커서를 구분자 뒤에 위치시킨다.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPlainTextEdit

from ..core.prompt_token import PromptToken, token_at_cursor, trailing_word
from ..core.tag_completer import TagCompleter, TagEntry, strip_weight_prefix
from ..core.wildcards.catalog import WildcardCatalog


class TagCompleterDropdown:
    """QCompleter-style popup attached to a QPlainTextEdit for tag suggestions.

    Parameters
    ----------
    text_edit : QPlainTextEdit
        The prompt input widget to attach to.
    completer : TagCompleter | None
        태그 완성기 (이미 로드되어 있어야 한다). None이면 태그는 제안하지 않는다.
    wildcards : WildcardCatalog | None
        와일드카드 이름 목록. None이면 `__`/`##` 자동완성을 하지 않는다.

    둘은 읽는 곳이 달라(태그 DB 파일 / 와일드카드 폴더) 옵션에서도 따로 켜고 끈다.
    그래서 어느 쪽이든 None일 수 있고, 남은 쪽만으로도 동작한다.
    """

    def __init__(
        self,
        text_edit: QPlainTextEdit,
        completer: TagCompleter | None = None,
        wildcards: WildcardCatalog | None = None,
    ) -> None:
        self._text_edit = text_edit
        self._completer = completer
        self._wildcards = wildcards
        #: 지금 띄운 후보를 고르면 갈아 끼울 자리 — 팝업을 채울 때마다 함께 갱신한다.
        #: (구간 전체가 아니라 마지막 낱말로 찾았으면 그 낱말의 시작이다.)
        self._replace_start = 0
        #: 그때 되살릴 가중치 접두사 ("1.5::"). 낱말로 찾은 경우엔 접두사가 치환 범위
        #: 밖에 남아 있으므로 빈 문자열이다.
        self._replace_prefix = ""

        # Popup list widget
        self._popup = QListWidget()
        self._popup.setWindowFlags(Qt.WindowType.ToolTip)
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._popup.setMaximumHeight(200)
        self._popup.setMinimumWidth(200)

        # Connect signals
        self._text_edit.textChanged.connect(self._on_text_changed)
        # 커서만 움직여도(방향키·마우스 클릭) 후보를 다시 본다 — 글자를 바꾸지 않으면
        # textChanged가 안 울려, 커서를 옮긴 자리에 맞지 않는 팝업이 그대로 남았다.
        self._text_edit.cursorPositionChanged.connect(self._on_cursor_moved)
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
        try:
            self._text_edit.cursorPositionChanged.disconnect(self._on_cursor_moved)
        except RuntimeError:
            pass
        self._text_edit.removeEventFilter(self._key_filter)
        self._key_filter.deleteLater()
        self._popup.hide()
        self._popup.deleteLater()

    # ── Token extraction ──────────────────────────────────

    @staticmethod
    def extract_current_token(text: str, cursor_pos: int) -> tuple[str, int]:
        """커서 앞 토큰과 그 시작 위치 — `core.prompt_token.token_at_cursor()`의 얇은 껍데기.

        Returns
        -------
        tuple[str, int]
            (token_text, token_start_position_in_full_text)
        """
        token = token_at_cursor(text, cursor_pos)
        return token.text, token.start

    # ── Signal handlers ───────────────────────────────────

    def _on_text_changed(self) -> None:
        """Handle text changes: extract token and show/hide popup."""
        self._refresh_popup()

    def _on_cursor_moved(self) -> None:
        """커서만 움직였을 때(글자 변화 없이) 팝업을 다시 맞춘다.

        팝업이 떠 있지 않으면 아무것도 하지 않는다 — 커서를 움직였다는 것만으로
        새 팝업을 띄우면, 예전 태그를 클릭해 커서가 그 안으로 들어갈 때마다 후보가
        따라 떠서 성가시다. 이미 떠 있을 때만, 옮겨 간 자리에 맞는지 다시 본다.
        """
        if self._popup.isVisible():
            self._refresh_popup()

    def _refresh_popup(self) -> None:
        """커서 앞 토큰으로 후보를 다시 채우거나, 없으면 팝업을 감춘다."""
        cursor = self._text_edit.textCursor()
        token = token_at_cursor(self._text_edit.toPlainText(), cursor.position())

        items = self._wildcard_items(token)
        start, prefix = token.start, ""
        if not items:
            items, start, prefix = self._tag_completions(token)
        if not items:
            self._popup.hide()
            return

        self._replace_start, self._replace_prefix = start, prefix
        self._show_items(items)

    def _wildcard_items(self, token: PromptToken) -> list[tuple[str, str]]:
        """와일드카드를 치는 중이면 이름 후보, 아니면 빈 목록.

        태그와 달리 **글자 수 제한이 없다** — 여는 기호만 쳐도 폴더 안에 무엇이 있는지
        보여 주는 것이 목적이기 때문이다.
        """
        if self._wildcards is None or token.wildcard is None:
            return []
        insert = token.wildcard.insertion
        return [(insert(name), insert(name)) for name in self._wildcards.suggest(token.wildcard.keyword)]

    def _tag_completions(self, token: PromptToken) -> tuple[list[tuple[str, str]], int, str]:
        """(후보 목록, 치환 시작 위치, 되살릴 가중치 접두사).

        구간 전체로 먼저 찾는다 — `blue ey`처럼 낱말이 여럿인 태그가 걸려야 하기 때문이다.
        거기서 아무것도 안 나오면 **마지막 낱말**로 한 번 더 찾는다: 쉼표 없이 이어 쓴
        `1girl, solo masterpi`에서 `masterpiece`가 뜨고, 갈아 끼우는 것도 그 낱말뿐이다.
        """
        none = ([], token.start, "")
        if self._completer is None or not self._completer.is_enabled:
            return none
        if token.wildcard is not None:
            return none  # 와일드카드 자리에 태그를 제안하지 않는다

        for candidate in (token, trailing_word(token)):
            if candidate is None:
                continue
            weight, keyword = strip_weight_prefix(candidate.text)  # "1.5::blue" → "blue"로 검색
            if len(keyword) < 2:
                continue
            entries = self._completer.suggest(keyword)
            if entries:
                return ([self._tag_item(entry) for entry in entries], candidate.start, weight)
        return none

    @staticmethod
    def _tag_item(entry: TagEntry) -> tuple[str, str]:
        """태그 한 줄 — 언더스코어는 넣을 때 공백으로 바꾼다 (NAI 프롬프트 표기)."""
        return (f"{entry.name}  ({entry.post_count:,})", entry.name.replace("_", " "))

    def _show_items(self, items: list[tuple[str, str]]) -> None:
        """Populate and position the popup with (label, completion) pairs."""
        self._popup.clear()
        for label, completion in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, completion)
            self._popup.addItem(item)

        # Position popup below the cursor
        cursor_rect = self._text_edit.cursorRect()
        global_pos = self._text_edit.mapToGlobal(QPoint(cursor_rect.x(), cursor_rect.bottom()))
        self._popup.move(global_pos)
        self._popup.setCurrentRow(0)
        self._popup.show()

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        """Replace partial token with the selected value + ', '."""
        completion = item.data(Qt.ItemDataRole.UserRole)
        if not completion:
            self._popup.hide()
            return

        self._apply_completion(completion)
        self._popup.hide()

    def _apply_completion(self, completion: str) -> None:
        """Replace the current partial token with `completion` + ', ' and reposition cursor.

        갈아 끼울 자리는 후보를 띄울 때 정해 둔 것을 그대로 쓴다 (`_replace_start`) — 구간
        전체로 찾았는지 마지막 낱말로 찾았는지에 따라 다르기 때문이다. 사용자가 적어 둔
        가중치 접두사("1.5::")는 치환 범위 안에 있을 때만 되살린다 (V4.5와 같은 동작).
        """
        cursor = self._text_edit.textCursor()
        cursor_pos = cursor.position()

        token_start = min(self._replace_start, cursor_pos)

        # Build replacement: [가중치 접두사 +] 완성된 값 + ", "
        replacement = self._replace_prefix + completion + ", "

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

        if obj is not self._text_edit:
            return False

        # 입력창이 포커스를 잃거나 숨겨지면(다른 위젯·창으로 이동, 탭 전환 등)
        # 팝업은 별도의 최상위 창이라 저절로 사라지지 않는다 — 손으로 감춘다.
        # 이것이 "자동완성창이 계속 남는" 제보의 주 원인이다.
        if event.type() in (QEvent.Type.FocusOut, QEvent.Type.Hide):
            self._popup.hide()
            return False

        if not self._popup.isVisible():
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
