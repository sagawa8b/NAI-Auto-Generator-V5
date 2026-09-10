"""아레나 창 상태줄 — 지금 상태 한 줄 + 다음 할 일 + 진행 막대.

탭 **밖에**, 창 **바닥**에 둔다. 어느 탭을 보고 있든 같은 자리에서 같은 것을 알려 줘야
하고, 자리는 메인 창의 상태 표시줄과 같은 쪽이어야 눈이 헤매지 않는다 (한때 창 맨 위에
뒀다가 저장소 주인 요청으로 아래로 내렸다).

세 가지를 한 줄에 담는다.

- **숫자** — 작가 · 조합 · 그림 · 대결 · 미확정. 탭을 들락거리며 세지 않아도 된다.
- **다음 할 일** — `core/arena/guidance.py`가 상태를 보고 정한 단계. 누르면 그 탭으로
  간다. **동작을 대신 하지는 않는다** — 크레딧을 쓰는 단계가 섞여 있어서, 여기서
  대신 눌러 주면 사용자가 의도하지 않은 소모가 일어날 수 있다.
- **진행 막대와 한 줄 설명** — 생성이 어디까지 갔는지.

넷을 한 줄에 담고, `닫기`·`창 크기 초기화` 버튼과 같은 줄에 나란히 놓는다.

진행 막대와 설명 라벨은 `ArenaDialog`가 `progress` · `status_label`이라는 이름으로
그대로 들고 쓴다 (이 창을 검증하는 테스트가 그 이름으로 붙어 있다).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.guidance import (
    STEP_ADD_ARTISTS,
    STEP_EVOLVE,
    STEP_GENERATE,
    STEP_MAKE_COMBOS,
    STEP_MATCH,
    PipelineStatus,
)
from ...core.i18n.manager import I18nManager
from .style import mark_primary

#: 단계 → i18n 키. 개수를 함께 보여 주는 단계는 `{}`가 하나 들어 있다.
_STEP_KEYS = {
    STEP_ADD_ARTISTS: "arena.step_add_artists",
    STEP_MAKE_COMBOS: "arena.step_make_combos",
    STEP_GENERATE: "arena.step_generate",
    STEP_MATCH: "arena.step_match",
    STEP_EVOLVE: "arena.step_evolve",
}

#: 개수를 라벨에 넣는 단계.
_COUNTED_STEPS = (STEP_GENERATE, STEP_MATCH)

#: 진행 막대 폭 (논리 픽셀). 숫자 줄을 밀어내지 않을 만큼만.
_PROGRESS_WIDTH = 180

#: 숫자와 상태 메시지 사이의 칸막이.
_DIVIDER = "/"


class ArenaHeader(QWidget):
    """창 바닥의 상태 줄."""

    #: 다음 할 일 버튼을 눌렀다 — 값은 `guidance.STEP_*`.
    step_requested = Signal(str)

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._step = STEP_ADD_ARTISTS
        self._status: PipelineStatus | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 창 바닥에 붙으므로 구분선은 **위**에 둔다.
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        row = QHBoxLayout()
        self.counts_label = QLabel()
        row.addWidget(self.counts_label)

        # 숫자와 설명 사이의 칸막이. 설명이 없을 때는 숨긴다.
        self.divider_label = QLabel(_DIVIDER)
        self.divider_label.setVisible(False)
        row.addWidget(self.divider_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        # 긴 메시지가 `다음` 버튼을 창 밖으로 밀지 않게 — 남는 폭만 쓰고 접힌다.
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status_label, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedWidth(_PROGRESS_WIDTH)
        self.progress.setFormat("%v / %m")
        row.addWidget(self.progress)

        self.next_label = QLabel()
        row.addWidget(self.next_label)
        self.next_button = QPushButton()
        mark_primary(self.next_button)
        self.next_button.clicked.connect(lambda: self.step_requested.emit(self._step))
        row.addWidget(self.next_button)
        layout.addLayout(row)

        self.retranslate()

    # ── 바깥에서 부르는 것 ──────────────────────────────────────────────

    def set_message(self, message: str) -> None:
        """한 줄 설명을 갈아 끼운다. 비어 있으면 앞의 칸막이도 숨긴다."""
        self.status_label.setText(message)
        self.divider_label.setVisible(bool(message))

    def set_status(self, status: PipelineStatus) -> None:
        """숫자와 다음 할 일을 상태에 맞춰 다시 쓴다."""
        self._status = status
        self._step = status.step
        self._apply_status()

    def retranslate(self) -> None:
        self.next_label.setText(self._i18n.get_text("arena.next_label"))
        self.next_button.setToolTip(self._i18n.get_text("arena.next_hint"))
        self._apply_status()

    # ── 내부 ────────────────────────────────────────────────────────────

    def _apply_status(self) -> None:
        tr = self._i18n.get_text
        status = self._status
        if status is None:
            # 아직 상태를 받기 전 (생성자에서 부르는 `retranslate`). 숫자는 비워 두고
            # 라벨만 언어에 맞춰 둔다.
            self.counts_label.setText("")
        else:
            self.counts_label.setText(
                tr("arena.pipeline").format(
                    status.artists, status.combos, status.ready, status.matches, status.unsettled
                )
            )
        text = tr(_STEP_KEYS[self._step])
        if self._step in _COUNTED_STEPS:
            text = text.format(0 if status is None else status.step_count)
        self.next_button.setText(text)


__all__ = ["ArenaHeader"]
