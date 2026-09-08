"""아레나 탭의 공통 계약 — 다이얼로그가 탭을 다루는 유일한 인터페이스.

옵션 창의 `OptionsPage`와 같은 생각이다: 탭마다 위젯 구성은 제각각이지만, 바깥에서
부르는 메서드는 네 개로 고정한다. 그래야 다이얼로그가 탭 목록을 루프로 돌 수 있다.

- `refresh()`   상태(작가·조합)가 바뀌었으니 화면을 다시 그린다
- `commit()`    위젯 값을 `settings.arena`에 되쓴다 (창을 닫거나 탭을 떠날 때)
- `retranslate()` 언어가 바뀌었으니 문자열을 다시 넣는다
- `KEY`         탭 식별자 (i18n 키와 테스트가 쓴다)
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings
from ...services.arena_service import ArenaService


class ArenaTab(QWidget):
    """아레나 다이얼로그의 탭 하나."""

    #: 작가 명단이나 조합 목록이 바뀌었다 — 다이얼로그가 다른 탭까지 새로 그린다.
    state_changed = Signal()
    #: 다이얼로그 하단 상태줄에 띄울 한 줄.
    status_message = Signal(str)
    #: 그림이 부족하다 — 다이얼로그가 조합 생성 탭에 뽑기를 시킨다.
    #: 크레딧을 쓰는 동작이라 사용자가 자동 뽑기를 켠 경우에만 나간다.
    request_prefetch = Signal()

    KEY = ""

    def __init__(
        self,
        i18n: I18nManager,
        settings: AppSettings,
        service: ArenaService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._settings = settings
        self._service = service

    # ── 하위 클래스가 채운다 ──────────────────────────────────────────────

    def refresh(self) -> None:
        """상태 → 화면."""

    def commit(self) -> None:
        """화면 → `settings.arena`."""

    def retranslate(self) -> None:
        """언어 변경 반영."""

    def on_arena_event(self, event) -> None:
        """아레나 이벤트(생성 진행·완료). 다이얼로그가 모든 탭에 돌린다."""

    # ── 편의 ────────────────────────────────────────────────────────────

    @property
    def tr(self):
        return self._i18n.get_text

    @property
    def arena(self):
        """`settings.arena` — 탭이 만지는 조작값."""
        return self._settings.arena

    @property
    def state(self):
        """작가 명단과 조합 목록."""
        return self._service.state


__all__ = ["ArenaTab"]
