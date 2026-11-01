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

태그 지식 지표(`TagKnowledgeSource`)를 붙이면 태그 후보 오른쪽에 원 마커를 그린다 — 진할수록
지금 고른 모델이 그 태그를 잘 안다 (NovelAI 웹의 Tag Knowledge Indicators, `core/tag_knowledge.py`).
값은 서버에 물어 오므로 입력이 잠깐 멈춘 뒤(`KNOWLEDGE_DEBOUNCE_MS`) 작업 스레드에서 받고, 받으면
팝업을 다시 채운다. 서버가 그 입력으로 시작한다고 돌려준 태그는 로컬 DB 후보보다 **앞에** 온다
(모델이 아는 태그부터 보이게). 서버가 돌려주지 않은 태그에는 마커를 그리지 않는다 — 서버는 열 개
남짓만 돌려주므로 "목록에 없음"이 "모름"이라는 뜻은 아니다.

팝업 오른쪽 아래의 손잡이(`PopupResizeGrip`)를 끌면 크기가 바뀌고, 놓는 순간 QSettings
(`POPUP_SIZE_KEY`)에 남는다. 크기는 팝업을 띄울 때마다 거기서 읽으므로 프롬프트·네거티브·
캐릭터 슬롯이 모두 같은 크기를 쓰고, 다음 실행에도 그대로다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QMouseEvent, QPainter, QPaintEvent, QPen, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ..core.prompt_token import PromptToken, token_at_cursor, trailing_word
from ..core.tag_completer import TagCompleter, TagEntry, strip_weight_prefix
from ..core.tag_knowledge import TagKnowledge, TagKnowledgeService, knowledge_level, normalize_tag
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

#: 태그 지식 값(서버의 `count`)을 담는 항목 데이터 역할. 없으면 마커를 그리지 않는다.
KNOWLEDGE_ROLE = Qt.ItemDataRole.UserRole + 1

#: 입력이 이만큼(ms) 멈춰야 서버에 묻는다 — 글자마다 요청하지 않게.
KNOWLEDGE_DEBOUNCE_MS = 250


@dataclass(frozen=True)
class TagKnowledgeSource:
    """드롭다운이 태그 지식 지표를 그리는 데 필요한 것.

    `model`은 지금 고른 모델의 API 이름(`nai-diffusion-5-full` 등)을 돌려준다 — 모델을 바꾸면
    다음 입력부터 그 모델 값으로 묻는다. `tooltip`은 인지도 값 → 마커 툴팁 문구.
    """

    service: TagKnowledgeService
    model: Callable[[], str]
    tooltip: Callable[[int], str]


class KnowledgeMarkerDelegate(QStyledItemDelegate):
    """`KNOWLEDGE_ROLE`이 있는 줄 오른쪽에 원 마커를 그린다.

    마커는 글자색으로 채우고 진하기만 인지도에 따라 바꾼다 — 밝은·어두운 테마 모두에서 "진할수록
    잘 안다"로 읽힌다 (웹 UI는 어두운 배경에 흰 원). 테두리는 늘 그려 0에 가까워도 자리가 보인다.
    """

    DIAMETER = 9
    MARGIN = 6
    #: 스타일이 글자 앞뒤에 두는 여백 (줄일 폭을 셀 때 뺀다).
    TEXT_PADDING = 12

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        count = index.data(KNOWLEDGE_ROLE)
        if count is None:
            super().paint(painter, option, index)
            return
        # 줄(선택 표시 포함)은 스타일이 평소대로 끝까지 그리게 두고, 글자만 원 자리 앞에서
        # 줄인다. 선택 막대를 직접 칠하면 Windows 11 스타일의 옅은 선택 표시와 어긋난다.
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        strip = self.DIAMETER + 2 * self.MARGIN
        room = option.rect.width() - strip - self.TEXT_PADDING
        if item.fontMetrics.horizontalAdvance(item.text) > room:
            item.text = item.fontMetrics.elidedText(item.text, Qt.TextElideMode.ElideMiddle, room)
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, widget)

        # 선택된 줄도 글자색을 따른다 — Windows 11 스타일은 선택해도 글자가 어둡다
        base = option.palette.text().color()
        fill = QColor(base)
        fill.setAlphaF(knowledge_level(int(count)))
        outline = QColor(base)
        outline.setAlphaF(0.6)
        rect = option.rect
        circle = QRectF(
            rect.right() - self.MARGIN - self.DIAMETER + 1,
            rect.center().y() - self.DIAMETER / 2 + 0.5,
            self.DIAMETER,
            self.DIAMETER,
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(outline, 1))
        painter.setBrush(fill)
        painter.drawEllipse(circle)
        painter.restore()


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
    knowledge : TagKnowledgeSource | None
        태그 지식 지표. None이면 서버에 묻지 않고 마커도 그리지 않는다.

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
        knowledge: TagKnowledgeSource | None = None,
    ) -> None:
        self._text_edit = text_edit
        self._completer = completer
        self._wildcards = wildcards
        self._lines = lines
        self._settings = settings
        self._knowledge = knowledge
        #: 지금 띄운 태그 후보의 인지도 (정규화한 태그 → count). 팝업을 채울 때마다 새로 만든다.
        self._knowledge_counts: dict[str, int] = {}
        #: 입력이 멈추면 물을 (모델, 입력).
        self._pending_knowledge: tuple[str, str] | None = None
        #: Esc로 닫은 뒤 다시 치기 전까지는 서버 값이 와도 팝업을 다시 띄우지 않는다.
        self._dismissed = False
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
        self._marker_delegate = KnowledgeMarkerDelegate(self._popup)
        self._popup.setItemDelegate(self._marker_delegate)

        self._knowledge_timer: QTimer | None = None
        self._knowledge_bridge = None
        if knowledge is not None:
            from .qt_bridge import TagKnowledgeBridge

            self._knowledge_timer = QTimer(text_edit)
            self._knowledge_timer.setSingleShot(True)
            self._knowledge_timer.setInterval(KNOWLEDGE_DEBOUNCE_MS)
            self._knowledge_timer.timeout.connect(self._request_knowledge)
            self._knowledge_bridge = TagKnowledgeBridge(text_edit)
            self._knowledge_bridge.result_ready.connect(self._on_knowledge_ready)

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
        if self._knowledge_timer is not None:
            self._knowledge_timer.stop()
            self._knowledge_timer.deleteLater()
            self._knowledge_timer = None
        if self._knowledge_bridge is not None:
            # 늦게 도착한 결과가 지운 팝업을 건드리지 않게 연결을 끊는다
            self._knowledge_bridge.result_ready.disconnect(self._on_knowledge_ready)
            self._knowledge_bridge.deleteLater()
            self._knowledge_bridge = None
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
        self._dismissed = False
        self._refresh_popup()

    def _on_cursor_moved(self) -> None:
        """커서만 움직였을 때(글자 변화 없이) 팝업을 다시 맞춘다.

        팝업이 떠 있지 않으면 아무것도 하지 않는다 — 커서를 움직였다는 것만으로
        새 팝업을 띄우면, 예전 태그를 클릭해 커서가 그 안으로 들어갈 때마다 후보가
        따라 떠서 성가시다. 이미 떠 있을 때만, 옮겨 간 자리에 맞는지 다시 본다.
        """
        if self._popup.isVisible():
            self._refresh_popup()

    def _refresh_popup(self, keep_selection: bool = False) -> None:
        """커서 앞 토큰으로 후보를 다시 채우거나, 없으면 팝업을 감춘다.

        `keep_selection`이면 고르고 있던 후보를 그대로 둔다 — 서버 값이 도착해 다시 채울 때
        방향키로 내려가 있던 자리가 맨 위로 튀지 않게.
        """
        cursor = self._text_edit.textCursor()
        token = token_at_cursor(self._text_edit.toPlainText(), cursor.position())

        self._knowledge_counts = {}
        items = self._wildcard_items(token)
        start, prefix = token.start, ""
        if not items:
            items, start, prefix = self._tag_completions(token)
        if not items:
            self._popup.hide()
            return

        self._replace_start, self._replace_prefix = start, prefix
        self._show_items(items, keep_selection=keep_selection)

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
        if not tags_on and self._lines is None and self._knowledge is None:
            return none
        if token.wildcard is not None:
            return none  # 와일드카드 자리에 태그를 제안하지 않는다

        model = self._knowledge.model() if self._knowledge is not None else ""
        #: 서버에 물을 입력 — 구간 전체가 먼저다. `mika (track`을 쳤으면 낱말 `(track`이 아니라
        #: 구간 전체를 물어야 `mika (track) (blue archive)`가 나온다.
        ask: list[str] = []
        for candidate in (token, trailing_word(token)):
            if candidate is None:
                continue
            weight, keyword = strip_weight_prefix(candidate.text)  # "1.5::blue" → "blue"로 검색
            if len(keyword) < 2:
                continue
            ask.append(keyword)
            items = self._line_items(keyword)
            taken = {normalize_tag(completion) for _label, completion, _color in items}
            local = self._completer.suggest(keyword) if tags_on else []
            for known in self._server_tags(model, keyword):
                key = normalize_tag(known.tag)
                self._knowledge_counts[key] = known.count
                if known.is_prefix_match and key not in taken and len(items) < MAX_TOTAL_SUGGESTIONS:
                    # 로컬 DB에도 있으면 그쪽 문구(게시물 수)를 쓰고, 자리만 앞으로 당긴다
                    entry = next((e for e in local if normalize_tag(e.name) == key), None)
                    items.append(self._tag_item(entry) if entry else (known.tag, known.tag, None))
                    taken.add(key)
            for entry in local:
                if len(items) >= MAX_TOTAL_SUGGESTIONS:
                    break
                item = self._tag_item(entry)
                if normalize_tag(item[1]) not in taken:
                    items.append(item)
            if items:
                self._schedule_knowledge(model, ask)
                return (items, candidate.start, weight)
        self._schedule_knowledge(model, ask)
        return none

    # ── 태그 지식 지표 ────────────────────────────────────

    def _server_tags(self, model: str, keyword: str) -> list[TagKnowledge]:
        """캐시에 있는 서버 후보 (없거나 지표가 꺼져 있으면 빈 목록)."""
        if self._knowledge is None or not model:
            return []
        return self._knowledge.service.cached(model, keyword) or []

    def _schedule_knowledge(self, model: str, keywords: list[str]) -> None:
        """`keywords` 중 캐시에 없는 첫 입력을, 입력이 멈춘 뒤 묻도록 예약한다.

        답이 오면 팝업을 다시 채우며 이 함수가 또 불리므로, 구간 전체 → 마지막 낱말 순으로
        하나씩 묻게 된다. 입력이 바뀌면 예약도 바뀐다.
        """
        if self._knowledge_timer is None or not model:
            return
        service = self._knowledge.service
        missing = next((k for k in keywords if service.cached(model, k) is None), None)
        if missing is None:
            self._pending_knowledge = None
            self._knowledge_timer.stop()
            return
        self._pending_knowledge = (model, missing)
        self._knowledge_timer.start()

    def _request_knowledge(self) -> None:
        if self._pending_knowledge is None or self._knowledge is None or self._knowledge_bridge is None:
            return
        model, keyword = self._pending_knowledge
        self._pending_knowledge = None
        self._knowledge.service.request(model, keyword, self._knowledge_bridge)

    def _on_knowledge_ready(self, model: str, _keyword: str, _tags: object) -> None:
        """서버 값이 왔다 — 아직 그 입력란에서 치는 중이면 후보를 다시 채운다 (이번엔 캐시에서)."""
        if self._knowledge is None or model != self._knowledge.model():
            return
        if self._dismissed:
            return
        if self._popup.isVisible() or self._text_edit.hasFocus():
            self._refresh_popup(keep_selection=True)

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

    def _show_items(self, items: list[Item], keep_selection: bool = False) -> None:
        """Populate and position the popup with (label, completion, color) items."""
        previous = self._popup.currentItem()
        selected = previous.data(Qt.ItemDataRole.UserRole) if keep_selection and previous else None
        self._popup.clear()
        row = 0
        for label, completion, color in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, completion)
            if color is not None:
                item.setForeground(color)
            else:  # 등록 와일드카드 줄(색 있음)에는 마커를 그리지 않는다
                count = self._knowledge_counts.get(normalize_tag(completion))
                if count is not None and self._knowledge is not None:
                    item.setData(KNOWLEDGE_ROLE, count)
                    item.setToolTip(self._knowledge.tooltip(count))
            if completion == selected:
                row = self._popup.count()
            self._popup.addItem(item)

        # 다른 입력란의 팝업에서 바꾼 크기도 따라가게, 띄울 때마다 저장된 값을 읽는다
        if self._settings is not None:
            self._popup.resize(load_popup_size(self._settings))

        # Position popup below the cursor
        cursor_rect = self._text_edit.cursorRect()
        global_pos = self._text_edit.mapToGlobal(QPoint(cursor_rect.x(), cursor_rect.bottom()))
        self._popup.move(global_pos)
        self._popup.setCurrentRow(row)
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
            self._dismissed = True
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
    "KNOWLEDGE_DEBOUNCE_MS",
    "KNOWLEDGE_ROLE",
    "MAX_POPUP_SIZE",
    "MIN_POPUP_SIZE",
    "POPUP_SIZE_KEY",
    "SOURCE_COLORS",
    "KnowledgeMarkerDelegate",
    "PopupResizeGrip",
    "TagCompleterDropdown",
    "TagKnowledgeSource",
    "clamp_popup_size",
    "load_popup_size",
    "save_popup_size",
    "source_color",
]
