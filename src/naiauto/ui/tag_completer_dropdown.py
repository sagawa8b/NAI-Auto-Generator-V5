"""프롬프트 자동완성 드롭다운 위젯 (태그 + 와일드카드).

프롬프트 QPlainTextEdit에 부착되어 textChanged 시그널을 감시하고, 커서 앞에서 입력 중인
토큰을 `core.prompt_token.token_at_cursor()`로 읽는다 (구분자와 와일드카드 여는 기호를
가려내는 규칙은 그쪽에 있다).

토큰의 종류에 따라 목록이 갈린다:

- `__` / `__=` / `##`로 여는 토큰 → **와일드카드 이름** (`WildcardCatalog`). 여는 기호만
  쳐도 폴더 안 목록이 그대로 뜨고, 고르면 닫는 기호까지 붙는다 (`__folder/name__`).
- 그 밖 → **등록 와일드카드의 줄** (`WildcardLineSource`) + **태그** (`TagCompleter.suggest()`).
  등록 줄이 먼저 오고, 출처 와일드카드마다 색과 꼬리표(`[1_madoka]`)로 구분한다. 구간 전체로
  못 찾으면 **마지막 낱말**로 한 번 더 찾는다 — 쉼표 없이 이어 쓴 `1girl, solo masterpi`에서도
  뜨게 하려는 것이다 (`blue eyes`처럼 낱말이 여럿인 태그를 위해 공백은 경계로 두지 않는다).

선택 시 부분 토큰을 완성된 값 + ", "로 교체하고 커서를 구분자 뒤에 위치시킨다.

팝업 오른쪽 아래의 손잡이(`PopupResizeGrip`)를 끌면 크기가 바뀌고, 놓는 순간 QSettings
(`POPUP_SIZE_KEY`)에 남는다. 크기는 팝업을 띄울 때마다 거기서 읽으므로 프롬프트·네거티브·
캐릭터 슬롯이 모두 같은 크기를 쓰고, 다음 실행에도 그대로다.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QMouseEvent, QPainter, QPaintEvent, QPen, QTextCursor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPlainTextEdit, QWidget

from ..core.prompt_token import PromptToken, token_at_cursor, trailing_word
from ..core.tag_completer import TagCompleter, TagEntry, strip_weight_prefix
from ..core.wildcards.catalog import WildcardCatalog
from ..core.wildcards.line_source import WildcardLine, WildcardLineSource

#: 등록 와일드카드 줄 후보를 한 번에 보여 줄 최대 수 — 나머지 자리는 태그 몫이다.
MAX_LINE_SUGGESTIONS = 10

#: 태그와 합친 목록의 최대 길이 (태그 자동완성 단독일 때와 같은 상한).
MAX_TOTAL_SUGGESTIONS = 20

#: 줄 후보 문구에서 줄 원문을 자르는 길이 — 넣을 때는 자르지 않는다. 화면에 맞추는 일은
#: 팝업 너비에 따라 Qt가 가운데를 줄여(`ElideMiddle`) 하므로, 여기서는 터무니없이 긴 줄만 막는다.
MAX_LINE_LABEL = 300

#: 등록 와일드카드별 글자색. 등록 순서대로 돌려 쓴다. `prompt_highlighter`처럼 밝은·어두운
#: 테마 모두에서 읽히는 중간 채도의 고정색이다 (팔레트에 따라 바꾸지 않는다).
SOURCE_COLORS: tuple[QColor, ...] = (
    QColor(230, 126, 34),  # 주황
    QColor(46, 160, 67),  # 초록
    QColor(155, 89, 182),  # 보라
    QColor(214, 69, 101),  # 분홍
    QColor(23, 162, 184),  # 청록
    QColor(191, 144, 0),  # 황토
)


def source_color(index: int) -> QColor:
    """등록 순서 `index`번째 와일드카드의 색 (옵션 화면 목록과 드롭다운이 같은 색을 쓴다)."""
    return SOURCE_COLORS[index % len(SOURCE_COLORS)]


#: 후보 한 줄 — (보여 줄 문구, 넣을 값, 글자색 또는 None).
Item = tuple[str, str, QColor | None]

#: 팝업 크기를 남기는 QSettings 키 (값은 `"너비x높이"`).
POPUP_SIZE_KEY = "ui/completer_popup_size"
#: 저장된 값이 없을 때의 크기 (예전 고정 크기와 비슷하게, 너비만 조금 넓혔다).
DEFAULT_POPUP_SIZE = QSize(320, 200)
#: 끌어서 줄이거나 늘릴 수 있는 범위.
MIN_POPUP_SIZE = QSize(200, 80)
MAX_POPUP_SIZE = QSize(1600, 1000)


def clamp_popup_size(width: int, height: int) -> QSize:
    """`MIN_POPUP_SIZE`~`MAX_POPUP_SIZE` 안으로 맞춘 크기."""
    return QSize(
        max(MIN_POPUP_SIZE.width(), min(MAX_POPUP_SIZE.width(), int(width))),
        max(MIN_POPUP_SIZE.height(), min(MAX_POPUP_SIZE.height(), int(height))),
    )


def load_popup_size(settings: QSettings | None) -> QSize:
    """저장된 팝업 크기. 없거나 읽을 수 없으면 기본 크기."""
    raw = settings.value(POPUP_SIZE_KEY) if settings is not None else None
    if isinstance(raw, str) and "x" in raw:
        width, _, height = raw.partition("x")
        try:
            return clamp_popup_size(int(width), int(height))
        except ValueError:
            pass
    return QSize(DEFAULT_POPUP_SIZE)


def save_popup_size(settings: QSettings | None, size: QSize) -> None:
    if settings is not None:
        settings.setValue(POPUP_SIZE_KEY, f"{size.width()}x{size.height()}")


class PopupResizeGrip(QWidget):
    """팝업 오른쪽 아래 모서리의 크기 조절 손잡이.

    팝업은 테두리 없는 툴팁 창이라 창 가장자리를 잡아 늘릴 수 없다. 그래서 목록 아래에 손잡이
    한 줄(`SIZE`) 자리를 비워 두고(`setViewportMargins`), 그 오른쪽 끝에 손잡이를 둔다 —
    마지막 후보나 스크롤 막대를 가리지 않게. 놓으면 `on_released(크기)`를 부른다.
    """

    SIZE = 16

    def __init__(self, popup: QListWidget, on_released: Callable[[QSize], None]) -> None:
        super().__init__(popup)
        self._popup = popup
        self._on_released = on_released
        self._drag_origin: QPoint | None = None
        self._drag_start_size = QSize()
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        popup.setViewportMargins(0, 0, 0, self.SIZE)
        popup.installEventFilter(self)

    def reposition(self) -> None:
        """목록 영역(스크롤 막대 제외) 바로 아래 오른쪽 끝으로 옮긴다."""
        viewport = self._popup.viewport().geometry()
        self.move(viewport.right() - self.SIZE + 1, viewport.bottom() + 1)
        self.raise_()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt 콜백 이름)
        if obj is self._popup and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            self.reposition()
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_start_size = self._popup.size()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            self._popup.resize(
                clamp_popup_size(
                    self._drag_start_size.width() + delta.x(), self._drag_start_size.height() + delta.y()
                )
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self._on_released(self._popup.size())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        """흔한 크기 조절 표시 — 오른쪽 아래로 모이는 빗금 세 줄."""
        painter = QPainter(self)
        painter.setPen(QPen(self.palette().mid().color(), 1))
        s = self.SIZE - 1
        for offset in (3, 6, 9):
            painter.drawLine(s, s - offset, s - offset, s)
        painter.end()


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
    lines : WildcardLineSource | None
        등록 와일드카드의 줄 목록. None이면 태그 자리에 줄 후보를 섞지 않는다.
    settings : QSettings | None
        팝업 크기를 읽고 남길 곳. None이면 끌어서 바꾼 크기가 이 드롭다운에만 남는다.

    셋은 읽는 곳이 달라(태그 DB 파일 / 와일드카드 폴더 / 등록한 와일드카드 파일) 옵션에서도
    따로 켜고 끈다. 그래서 어느 쪽이든 None일 수 있고, 남은 쪽만으로도 동작한다.
    """

    def __init__(
        self,
        text_edit: QPlainTextEdit,
        completer: TagCompleter | None = None,
        wildcards: WildcardCatalog | None = None,
        lines: WildcardLineSource | None = None,
        settings: QSettings | None = None,
    ) -> None:
        self._text_edit = text_edit
        self._completer = completer
        self._wildcards = wildcards
        self._lines = lines
        self._settings = settings
        #: 지금 띄운 후보를 고르면 갈아 끼울 자리 — 팝업을 채울 때마다 함께 갱신한다.
        #: (구간 전체가 아니라 마지막 낱말로 찾았으면 그 낱말의 시작이다.)
        self._replace_start = 0
        #: 그때 되살릴 가중치 접두사 ("1.5::"). 낱말로 찾은 경우엔 접두사가 치환 범위
        #: 밖에 남아 있으므로 빈 문자열이다.
        self._replace_prefix = ""

        # Popup list widget
        self._popup = QListWidget()
        # 팝업을 눌러도 활성 창이 되지 않게 한다. 그러지 않으면 Windows에서 손잡이·후보를
        # 누르는 순간 팝업 창이 활성화되며 입력란이 포커스를 잃고(FocusOut), 아래
        # `eventFilter`가 그 FocusOut을 보고 팝업을 닫아 버린다 (손잡이를 잡자마자 닫히던 문제).
        self._popup.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.WindowDoesNotAcceptFocus)
        self._popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 넘치는 문구는 가운데를 줄인다 — 끝을 자르면 줄 후보의 출처 꼬리표(`[이름]`)가 사라진다
        self._popup.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._popup.setMinimumSize(MIN_POPUP_SIZE)
        self._popup.setMaximumSize(MAX_POPUP_SIZE)
        self._popup.resize(load_popup_size(settings))
        self._grip = PopupResizeGrip(self._popup, self._on_popup_resized)

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

    def _wildcard_items(self, token: PromptToken) -> list[Item]:
        """와일드카드를 치는 중이면 이름 후보, 아니면 빈 목록.

        태그와 달리 **글자 수 제한이 없다** — 여는 기호만 쳐도 폴더 안에 무엇이 있는지
        보여 주는 것이 목적이기 때문이다.
        """
        if self._wildcards is None or token.wildcard is None:
            return []
        insert = token.wildcard.insertion
        return [
            (insert(name), insert(name), None) for name in self._wildcards.suggest(token.wildcard.keyword)
        ]

    def _tag_completions(self, token: PromptToken) -> tuple[list[Item], int, str]:
        """(후보 목록, 치환 시작 위치, 되살릴 가중치 접두사).

        구간 전체로 먼저 찾는다 — `blue ey`처럼 낱말이 여럿인 태그가 걸려야 하기 때문이다.
        거기서 아무것도 안 나오면 **마지막 낱말**로 한 번 더 찾는다: 쉼표 없이 이어 쓴
        `1girl, solo masterpi`에서 `masterpiece`가 뜨고, 갈아 끼우는 것도 그 낱말뿐이다.

        등록 와일드카드의 줄이 있으면 태그보다 **앞에** 온다 — 사용자가 직접 골라 둔 것이라
        의도가 더 분명하다. 줄과 같은 태그는 한 번만 보인다 (줄 쪽에 남긴다).
        """
        none: tuple[list[Item], int, str] = ([], token.start, "")
        tags_on = self._completer is not None and self._completer.is_enabled
        if not tags_on and self._lines is None:
            return none
        if token.wildcard is not None:
            return none  # 와일드카드 자리에 태그를 제안하지 않는다

        for candidate in (token, trailing_word(token)):
            if candidate is None:
                continue
            weight, keyword = strip_weight_prefix(candidate.text)  # "1.5::blue" → "blue"로 검색
            if len(keyword) < 2:
                continue
            items = self._line_items(keyword)
            if tags_on:
                taken = {completion.lower() for _label, completion, _color in items}
                for entry in self._completer.suggest(keyword):
                    if len(items) >= MAX_TOTAL_SUGGESTIONS:
                        break
                    item = self._tag_item(entry)
                    if item[1].lower() not in taken:
                        items.append(item)
            if items:
                return (items, candidate.start, weight)
        return none

    def _line_items(self, keyword: str) -> list[Item]:
        """등록 와일드카드의 줄 후보 — 출처마다 색을 달리한다."""
        if self._lines is None:
            return []
        order = {name: index for index, name in enumerate(self._lines.names)}
        return [
            self._line_item(line, source_color(order.get(line.source, 0)))
            for line in self._lines.suggest(keyword, limit=MAX_LINE_SUGGESTIONS)
        ]

    @staticmethod
    def _line_item(line: WildcardLine, color: QColor) -> Item:
        """줄 한 개 — 문구는 길면 자르고 출처 이름을 꼬리표로 붙인다. 넣는 값은 줄 원문 그대로."""
        text = line.text if len(line.text) <= MAX_LINE_LABEL else line.text[: MAX_LINE_LABEL - 1] + "…"
        return (f"{text}  [{line.source}]", line.text, color)

    @staticmethod
    def _tag_item(entry: TagEntry) -> Item:
        """태그 한 줄 — 언더스코어는 넣을 때 공백으로 바꾼다 (NAI 프롬프트 표기)."""
        return (f"{entry.name}  ({entry.post_count:,})", entry.name.replace("_", " "), None)

    def _show_items(self, items: list[Item]) -> None:
        """Populate and position the popup with (label, completion, color) items."""
        self._popup.clear()
        for label, completion, color in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, completion)
            if color is not None:
                item.setForeground(color)
            self._popup.addItem(item)

        # 다른 입력란의 팝업에서 바꾼 크기도 따라가게, 띄울 때마다 저장된 값을 읽는다
        if self._settings is not None:
            self._popup.resize(load_popup_size(self._settings))

        # Position popup below the cursor
        cursor_rect = self._text_edit.cursorRect()
        global_pos = self._text_edit.mapToGlobal(QPoint(cursor_rect.x(), cursor_rect.bottom()))
        self._popup.move(global_pos)
        self._popup.setCurrentRow(0)
        self._popup.show()

    def _on_popup_resized(self, size: QSize) -> None:
        """손잡이를 놓은 크기를 남긴다 (다음에 띄울 때·다음 실행에 그대로)."""
        save_popup_size(self._settings, size)
        self._restore_focus()

    def _pointer_over_popup(self) -> bool:
        """마우스가 지금 팝업 위에 있는지 — 팝업을 누르다 생긴 FocusOut을 가려낸다."""
        return self._popup.isVisible() and self._popup.frameGeometry().contains(QCursor.pos())

    def _restore_focus(self) -> None:
        """팝업을 누른 뒤 입력란으로 포커스를 돌려준다 — 이어서 바로 칠 수 있게."""
        window = self._text_edit.window()
        if not window.isActiveWindow():
            window.activateWindow()
        self._text_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        """Replace partial token with the selected value + ', '."""
        completion = item.data(Qt.ItemDataRole.UserRole)
        if not completion:
            self._popup.hide()
            return

        self._apply_completion(completion)
        self._popup.hide()
        self._restore_focus()

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
        # 단, 팝업 자체를 누르다 생긴 FocusOut은 예외다 (손잡이로 크기를 바꾸거나 후보를 클릭할
        # 때). 팝업이 활성화되지 않게 해 두었지만 플랫폼이 그래도 포커스를 옮기는 경우를 막는다 —
        # 놓으면 `_restore_focus()`가 입력란으로 되돌린다.
        if event.type() == QEvent.Type.FocusOut and self._pointer_over_popup():
            return False
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


__all__ = [
    "DEFAULT_POPUP_SIZE",
    "MAX_POPUP_SIZE",
    "MIN_POPUP_SIZE",
    "POPUP_SIZE_KEY",
    "SOURCE_COLORS",
    "PopupResizeGrip",
    "TagCompleterDropdown",
    "clamp_popup_size",
    "load_popup_size",
    "save_popup_size",
    "source_color",
]
