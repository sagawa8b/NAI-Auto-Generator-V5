"""빈 화면 안내 — 제목 + 이유 + 행동 버튼.

빈 표와 빈 카드가 "아무것도 없음"만 말하고 끝나던 자리를 대신한다. 처음 아레나를 켜면
여섯 탭이 전부 비어 있는데, 어느 화면도 다음에 할 일을 알려 주지 않았다.

세 조각으로 이루어진다.

- **제목** — 무엇이 없는지 (`겨룰 그림이 없습니다`)
- **이유** — 왜 없는지, 무엇을 하면 되는지 (`조합 12개가 그림을 기다립니다`)
- **행동** — 눌러서 그리로 가는 버튼. **없으면 버튼을 숨긴다** — 지금 할 수 있는 일이
  정말 없을 때 눌러 봐야 아무 일도 일어나지 않는 버튼을 두면 더 헷갈린다.

문자열은 넣는 쪽이 이미 번역해서 준다. 이 위젯은 i18n을 모른다 — 언어가 바뀌면 부모가
`set_content`를 다시 부른다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class EmptyState(QWidget):
    """비어 있는 이유와 다음 행동을 함께 보여 주는 안내."""

    #: 행동 버튼을 눌렀다.
    action_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)

        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)

        self.detail_label = QLabel()
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.detail_label)

        self.action_button = QPushButton()
        self.action_button.clicked.connect(self.action_clicked)
        self.action_button.setVisible(False)
        # 가운데에 두되 가로로 늘어나지 않게 한 겹 감싼다 — 늘어나면 안내가 아니라
        # 화면을 가로지르는 막대처럼 보인다.
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 8, 0, 0)
        holder_layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._action_holder = holder
        holder.setVisible(False)
        layout.addWidget(holder)

        layout.addStretch(1)

    def set_content(self, title: str, detail: str = "", action: str = "") -> None:
        """안내 내용을 채운다. `action`이 비면 버튼을 숨긴다."""
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self.action_button.setText(action)
        self.action_button.setVisible(bool(action))
        self._action_holder.setVisible(bool(action))


__all__ = ["EmptyState"]
