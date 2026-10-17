"""좌측 입력 패널의 섹션을 드래그로 재배치하는 컨테이너.

각 섹션(프롬프트·캐릭터 프롬프트·모델·해상도 …)을 `SectionFrame`으로 감싸고
`SectionList`에 순서대로 쌓는다. **잠금 상태**에서는 프레임이 본문만 보여 주므로
화면이 예전과 같다. 잠금을 풀면 각 섹션 위에 그립 줄이 나타나고, 그립을 끌어
다른 섹션 사이에 놓거나 그립에 포커스를 두고 Alt+↑/↓로 한 칸씩 옮긴다.

생성 바는 스크롤 밖에 고정되어야 하므로 목록에 넣지 않는다. 대신 `EdgeDock`이
스크롤 영역의 위나 아래 중 한 곳에 붙여 두고, 그립을 끌어 놓은 쪽(위/아래 절반)으로
옮긴다.

저장된 순서를 현재 섹션 목록에 맞추는 일은 Qt 없이 검증할 수 있게 순수 함수
(`normalize_order`)로 둔다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager

#: 섹션 드래그에 쓰는 MIME 형식. 값은 섹션 키(UTF-8)다.
SECTION_MIME = "application/x-naiauto-section"
#: 그립 툴팁 i18n 키.
GRIP_TOOLTIP_KEY = "ui.section_grip"
#: 생성 바 자리.
EDGE_TOP = "top"
EDGE_BOTTOM = "bottom"
EDGES = (EDGE_TOP, EDGE_BOTTOM)


def normalize_order(saved: Iterable[str], known: Sequence[str]) -> list[str]:
    """저장된 순서를 지금 있는 섹션 목록에 맞춘다.

    모르는 키(이전 버전에 있던 섹션)와 중복은 버리고, 저장된 순서에 없는 새 섹션은
    기본 순서(`known`)에서 바로 앞에 오던 섹션 뒤에 끼운다 — 새 버전에서 섹션이
    늘어도 사용자가 정한 순서는 그대로 두고 새 섹션만 제자리 근처에 나타난다.
    """
    known_set = set(known)
    order: list[str] = []
    for key in saved:
        if key in known_set and key not in order:
            order.append(key)
    for index, key in enumerate(known):
        if key in order:
            continue
        anchor = next((k for k in reversed(known[:index]) if k in order), None)
        order.insert(order.index(anchor) + 1 if anchor is not None else 0, key)
    return order


def section_title(text: str) -> str:
    """그립 줄 제목 — 입력 라벨용 문자열("모델:")을 재사용할 때 끝의 콜론을 뗀다."""
    return text.rstrip().rstrip(":：").rstrip()


def normalize_edge(value: str) -> str:
    """생성 바 자리 문자열을 검증한다 — 모르는 값은 기본(아래)."""
    return value if value in EDGES else EDGE_BOTTOM


class SectionGrip(QToolButton):
    """섹션을 끌어 옮기는 손잡이. 키보드로는 Alt+↑/↓로 한 칸씩 옮긴다."""

    move_requested = Signal(int)  # -1 = 위로, +1 = 아래로

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._press_pos: QPoint | None = None
        self.setText("⋮⋮")
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt 콜백)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt 콜백)
        if self._press_pos is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        distance = (event.position().toPoint() - self._press_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            return
        self._press_pos = None
        self.setDown(False)
        mime = QMimeData()
        mime.setData(SECTION_MIME, self._key.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        frame = self.parentWidget()
        while frame is not None and not isinstance(frame, SectionFrame):
            frame = frame.parentWidget()
        if frame is not None:
            drag.setPixmap(frame.grab().scaledToWidth(min(240, max(1, frame.width()))))
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt 콜백)
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt 콜백)
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            if event.key() == Qt.Key.Key_Up:
                self.move_requested.emit(-1)
                return
            if event.key() == Qt.Key.Key_Down:
                self.move_requested.emit(1)
                return
        super().keyPressEvent(event)


class SectionFrame(QFrame):
    """그립 줄 + 본문. 그립 줄은 잠금이 풀렸을 때만 보인다.

    본문이 이미 제목을 보여 주는 섹션(QGroupBox·접이식 섹션)은 `show_title=False`로
    그립만 둔다 — 같은 제목이 두 번 겹쳐 보이지 않게. 제목은 그립의 접근성 이름에는
    그대로 쓴다.

    본문을 숨기면(i2i·Enhance처럼 보기 메뉴로 켜고 끄는 패널) 프레임도 함께 숨는다 —
    잠금을 푼 동안 빈 그립 줄만 덩그러니 남지 않게.
    """

    def __init__(
        self,
        i18n: I18nManager,
        key: str,
        title_key: str,
        body: QWidget,
        parent: QWidget | None = None,
        *,
        show_title: bool = True,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self.key = key
        self._title_key = title_key
        self.body = body

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QWidget(self)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.grip = SectionGrip(key, self)
        self.title_label = QLabel(self.header)
        self.title_label.setStyleSheet("color: palette(mid);")
        # 패널을 좁힐 때 제목 길이가 하한이 되지 않게 (잘려도 되는 글이다).
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.grip)
        header_layout.addWidget(self.title_label, stretch=1)
        self.title_label.setVisible(show_title)
        if not show_title:
            header_layout.addStretch(1)
        layout.addWidget(self.header)
        layout.addWidget(body)

        self.header.setVisible(False)
        self.setVisible(not body.isHidden())
        body.installEventFilter(self)
        self.retranslate()

    def set_unlocked(self, unlocked: bool) -> None:
        self.header.setVisible(unlocked)
        self.setFrameShape(QFrame.Shape.StyledPanel if unlocked else QFrame.Shape.NoFrame)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt 콜백)
        if obj is self.body:
            if event.type() == QEvent.Type.HideToParent:
                self.setVisible(False)
            elif event.type() == QEvent.Type.ShowToParent:
                self.setVisible(True)
        return super().eventFilter(obj, event)

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        title = section_title(tr(self._title_key))
        self.title_label.setText(title)
        self.grip.setToolTip(tr(GRIP_TOOLTIP_KEY))
        self.grip.setAccessibleName(f"{title} — {tr(GRIP_TOOLTIP_KEY)}")


class _DropIndicator(QFrame):
    """드롭될 자리를 보여 주는 가로선."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setStyleSheet("background: palette(highlight);")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def show_at(self, y: int, width: int) -> None:
        self.setGeometry(0, max(0, y - 1), width, 3)
        self.raise_()
        self.show()


class SectionList(QWidget):
    """섹션 프레임을 세로로 쌓고 드래그&드롭·키보드로 순서를 바꾼다."""

    order_changed = Signal(list)

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._frames: dict[str, SectionFrame] = {}
        self._default_order: list[str] = []
        self._locked = True
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # 섹션은 위로 붙인다 — 남는 공간은 맨 끝 stretch가 갖는다.
        self._layout.addStretch(1)
        self._indicator = _DropIndicator(self)
        self.setAcceptDrops(True)

    # ── 섹션 등록 ────────────────────────────────────────

    def add_section(
        self, key: str, title_key: str, body: QWidget, *, show_title: bool = True
    ) -> SectionFrame:
        """섹션을 맨 끝에 추가한다. 추가한 순서가 기본 순서가 된다."""
        frame = SectionFrame(self._i18n, key, title_key, body, self, show_title=show_title)
        frame.set_unlocked(not self._locked)
        frame.grip.move_requested.connect(lambda delta, k=key: self.move_section(k, delta))
        self._frames[key] = frame
        self._default_order.append(key)
        self._layout.insertWidget(self._layout.count() - 1, frame)
        return frame

    def frame(self, key: str) -> SectionFrame:
        return self._frames[key]

    def default_order(self) -> list[str]:
        return list(self._default_order)

    # ── 순서 ─────────────────────────────────────────────

    def order(self) -> list[str]:
        keys: list[str] = []
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if isinstance(widget, SectionFrame):
                keys.append(widget.key)
        return keys

    def set_order(self, keys: Iterable[str]) -> None:
        """저장된 순서를 적용한다 (모르는 키는 버리고 빠진 키는 채운다)."""
        order = normalize_order(keys, self._default_order)
        if order == self.order():
            return
        for index, key in enumerate(order):
            self._layout.insertWidget(index, self._frames[key])
        self.order_changed.emit(order)

    def reset_order(self) -> None:
        self.set_order(self._default_order)

    def move_section(self, key: str, delta: int) -> None:
        """잠금이 풀려 있을 때 섹션을 `delta`칸 옮긴다 (키보드 조작)."""
        if self._locked:
            return
        order = self.order()
        index = order.index(key)
        target = max(0, min(len(order) - 1, index + delta))
        if target == index:
            return
        order.insert(target, order.pop(index))
        self.set_order(order)
        self._frames[key].grip.setFocus()

    def _drop_index(self, y: int, dragged: str) -> int:
        """`y`에 놓으면 들어갈 자리 — 드래그 중인 섹션을 뺀 목록 기준 인덱스."""
        keys = [k for k in self.order() if k != dragged]
        for index, key in enumerate(keys):
            frame = self._frames[key]
            if frame.isVisible() and y < frame.geometry().center().y():
                return index
        # 숨은 섹션(i2i 등)은 건너뛰고, 마지막 보이는 섹션 아래면 맨 끝.
        return len(keys)

    def _indicator_y(self, index: int, dragged: str) -> int:
        keys = [k for k in self.order() if k != dragged]
        visible_after = [self._frames[k] for k in keys[index:] if self._frames[k].isVisible()]
        if visible_after:
            return visible_after[0].geometry().top() - self._layout.spacing() // 2
        visible = [self._frames[k] for k in keys if self._frames[k].isVisible()]
        return visible[-1].geometry().bottom() + self._layout.spacing() // 2 if visible else 0

    # ── 잠금 ─────────────────────────────────────────────

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        for frame in self._frames.values():
            frame.set_unlocked(not self._locked)

    # ── 드래그&드롭 ──────────────────────────────────────

    def _dragged_key(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> str | None:
        mime = event.mimeData()
        if self._locked or mime is None or not mime.hasFormat(SECTION_MIME):
            return None
        key = bytes(mime.data(SECTION_MIME).data()).decode("utf-8")
        return key if key in self._frames else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt 콜백)
        if self._dragged_key(event) is None:
            event.ignore()
            return
        event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 (Qt 콜백)
        key = self._dragged_key(event)
        if key is None:
            event.ignore()
            return
        index = self._drop_index(int(event.position().y()), key)
        self._indicator.show_at(self._indicator_y(index, key), self.width())
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 (Qt 콜백)
        self._indicator.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt 콜백)
        self._indicator.hide()
        key = self._dragged_key(event)
        if key is None:
            event.ignore()
            return
        index = self._drop_index(int(event.position().y()), key)
        order = [k for k in self.order() if k != key]
        order.insert(index, key)
        self.set_order(order)
        event.acceptProposedAction()

    def retranslate(self) -> None:
        for frame in self._frames.values():
            frame.retranslate()


class EdgeDock(QWidget):
    """가운데 위젯(스크롤 영역) 위나 아래에 섹션 하나를 고정해 붙인다.

    붙은 섹션의 그립을 끌어 이 위젯의 위쪽 절반에 놓으면 위, 아래쪽 절반이면 아래로 간다.
    """

    edge_changed = Signal(str)

    def __init__(
        self,
        i18n: I18nManager,
        center: QWidget,
        key: str,
        title_key: str,
        body: QWidget,
        parent: QWidget | None = None,
        *,
        show_title: bool = True,
    ) -> None:
        super().__init__(parent)
        self._edge = EDGE_BOTTOM
        self._locked = True
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(center, stretch=1)
        self.frame = SectionFrame(i18n, key, title_key, body, self, show_title=show_title)
        self.frame.grip.move_requested.connect(self._on_move_requested)
        self._layout.addWidget(self.frame)
        self._indicator = _DropIndicator(self)
        self.setAcceptDrops(True)

    def edge(self) -> str:
        return self._edge

    def set_edge(self, edge: str) -> None:
        edge = normalize_edge(edge)
        if edge == self._edge:
            return
        self._edge = edge
        self._layout.removeWidget(self.frame)
        self._layout.insertWidget(0 if edge == EDGE_TOP else self._layout.count(), self.frame)
        self.edge_changed.emit(edge)

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.frame.set_unlocked(not self._locked)

    def _on_move_requested(self, delta: int) -> None:
        if not self._locked:
            self.set_edge(EDGE_TOP if delta < 0 else EDGE_BOTTOM)
            self.frame.grip.setFocus()

    def _accepts(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        mime = event.mimeData()
        if self._locked or mime is None or not mime.hasFormat(SECTION_MIME):
            return False
        return bytes(mime.data(SECTION_MIME).data()).decode("utf-8") == self.frame.key

    def _edge_at(self, y: float) -> str:
        return EDGE_TOP if y < self.height() / 2 else EDGE_BOTTOM

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt 콜백)
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 (Qt 콜백)
        if not self._accepts(event):
            event.ignore()
            return
        top = self._edge_at(event.position().y()) == EDGE_TOP
        self._indicator.show_at(0 if top else self.height() - 2, self.width())
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 (Qt 콜백)
        self._indicator.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt 콜백)
        self._indicator.hide()
        if not self._accepts(event):
            event.ignore()
            return
        self.set_edge(self._edge_at(event.position().y()))
        event.acceptProposedAction()

    def retranslate(self) -> None:
        self.frame.retranslate()


__all__ = [
    "EDGES",
    "EDGE_BOTTOM",
    "EDGE_TOP",
    "SECTION_MIME",
    "EdgeDock",
    "SectionFrame",
    "SectionGrip",
    "SectionList",
    "normalize_edge",
    "normalize_order",
    "section_title",
]
