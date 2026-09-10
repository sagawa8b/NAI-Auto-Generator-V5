"""그림체 아레나 창 — 탭 컨테이너.

작가를 조합해 그림을 뽑고(조합 생성), 1:1로 겨루게 해 점수를 매기고(월드컵),
상위 조합을 교배해 다음 세대를 만든다(진화). 탭 사이에 흐르는 것은 딱 하나,
`ArenaService.state`(작가 명단 + 조합 목록)다.

**모드리스**다 — 메인 창을 막지 않는다. 아레나가 그림을 뽑는 동안에도 사용자는
메인 창을 보고 있을 수 있어야 하고, 조합을 메인 프롬프트로 보내는 것도 창을 닫지
않고 해야 한다.

창 **바닥**에 `ArenaHeader`가 있다 — 지금 상태(작가·조합·그림·대결·미확정)와
**다음에 할 일**을 어느 탭에서나 같은 자리에서 보여 준다. 탭이 평평하게 놓여 있으면
"지금 뭘 해야 하지"를 사용자가 스스로 추론해야 하는데, 그 추론은
`core/arena/guidance.py`가 한 번 해 둔다. 진행 막대와 한 줄 설명도 여기 살고,
`닫기`·`창 크기 초기화`와 한 줄을 나눠 쓴다 (메인 창의 상태 표시줄과 같은 쪽이다).

이벤트 흐름:

    GenerationService ──(생성 이벤트)──> MainWindow
                                            │ arena.handle_event(e)  ← 아레나 잡이면 True
                                            ▼
                                       ArenaService ──(아레나 이벤트)──> 이 창 ──> 각 탭
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.arena.guidance import (
    STEP_ADD_ARTISTS,
    STEP_EVOLVE,
    STEP_GENERATE,
    STEP_MAKE_COMBOS,
    STEP_MATCH,
    pipeline_status,
)
from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings
from ...services.arena_service import (
    ArenaBatchFinished,
    ArenaBatchStarted,
    ArenaImageReady,
    ArenaService,
)
from ..widgets import enable_window_controls, ensure_on_screen
from .arena_tab import ArenaMatchTab
from .base import ArenaTab
from .build_tab import BuildTab
from .header import ArenaHeader
from .judge_tab import JudgeTab
from .result_tab import ResultTab
from .roster_tab import RosterTab
from .style import arena_stylesheet

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "arena/geometry"
_TAB_KEY = "arena/tab"

#: 처음 뜰 때의 크기. 화면이 이보다 작으면 화면에 맞춰 줄인다.
DEFAULT_SIZE = (1000, 760)

#: 더는 줄일 수 없는 크기. 이것도 화면보다 크면 화면에 맞춘다 — 최소 크기가
#: 화면보다 크면 창이 화면 밖으로 삐져나가고, 그 상태로는 줄일 수도 없다.
FLOOR_SIZE = (720, 560)


class ArenaDialog(QDialog):
    """그림체 아레나."""

    #: 고른 조합을 메인 창 프롬프트로 보낸다 (작가 블록 문자열).
    prompt_selected = Signal(str)

    #: 통계 결산 생성 요청 — (조합 목록, 조합당 장수). 메인 창이 자기 생성
    #: 파이프라인으로 돌려, 결과가 결과 폴더에 쌓이게 한다.
    finale_requested = Signal(list, int)

    def __init__(
        self,
        i18n: I18nManager,
        settings: AppSettings,
        service: ArenaService,
        request_provider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._settings = settings
        self._service = service
        self._qsettings = QSettings()

        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.roster_tab = RosterTab(i18n, settings, service, self)
        self.build_tab = BuildTab(i18n, settings, service, request_provider, self)
        self.match_tab = ArenaMatchTab(i18n, settings, service, self)
        self.judge_tab = JudgeTab(i18n, settings, service, self)
        self.result_tab = ResultTab(i18n, settings, service, self)
        self.result_tab.prompt_selected.connect(self.prompt_selected)
        self.result_tab.finale_requested.connect(self.finale_requested)
        self._tabs: list[ArenaTab] = [
            self.roster_tab,
            self.build_tab,
            self.match_tab,
            self.judge_tab,
            self.result_tab,
        ]
        for tab in self._tabs:
            self.tabs.addTab(tab, "")
            tab.state_changed.connect(self._on_state_changed)
            tab.status_message.connect(self.show_status)
            tab.request_prefetch.connect(self.build_tab.start_generation)
            tab.navigate_requested.connect(self.go_to_step)

        # 상태줄 — 지금 상태와 다음 할 일. **탭 밖에** 둬서 어느 탭을 보고 있든 같은
        # 자리에서 보인다. 메인 창의 상태 표시줄과 같은 쪽(아래)에 두고, 창 버튼과
        # 한 줄을 나눠 쓴다.
        self.header = ArenaHeader(i18n, self)
        self.header.step_requested.connect(self.go_to_step)
        self.progress = self.header.progress
        self.status_label = self.header.status_label

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.close)
        # 어느 탭에서든 누를 수 있게 창 바닥에 둔다 — 창이 화면 밖으로 나가면
        # 탭을 바꾸는 것조차 어려울 수 있다.
        self.reset_size_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ResetRole)
        self.reset_size_button.clicked.connect(self.reset_size)

        footer = QHBoxLayout()
        footer.addWidget(self.header, 1)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)

        self._service.subscribe(self._on_arena_event)
        self._i18n.subscribe(self._on_language_changed)

        # 버튼 등급(주 동작·위험 동작)은 창 하나에 QSS 한 벌로 붙인다. 색은 팔레트에서
        # 가져오므로 사용자 테마를 그대로 따른다.
        self.setStyleSheet(arena_stylesheet(self.palette()))

        # 오래 띄워 놓는 창이다 — 최소화·최대화 단추를 붙인다 (`show()` 전에).
        enable_window_controls(self)
        self._apply_minimum_size()
        self.resize(*self._default_size())
        self._restore_geometry()
        self.retranslate()
        self.refresh()

    # ── 바깥에서 부르는 것 ──────────────────────────────────────────────

    def refresh(self) -> None:
        """모든 탭과 상태줄을 상태에 맞춰 다시 그린다."""
        for tab in self._tabs:
            tab.refresh()
        self.header.set_status(pipeline_status(self._service.state))

    def go_to_step(self, step: str) -> bool:
        """상태줄의 `다음` 버튼 — 그 단계를 하는 탭으로 옮긴다.

        **동작을 대신 하지는 않는다.** 단계 중에 크레딧을 쓰는 것(`그림 뽑기`)이 섞여
        있어, 여기서 대신 눌러 주면 사용자가 의도하지 않은 소모가 일어난다. 데려다만
        주고 누르는 것은 사용자가 한다.
        """
        tab = {
            STEP_ADD_ARTISTS: self.roster_tab,
            STEP_MAKE_COMBOS: self.build_tab,
            STEP_GENERATE: self.build_tab,
            STEP_MATCH: self.match_tab,
            STEP_EVOLVE: self.result_tab,
        }.get(step)
        if tab is None:
            return False
        self.tabs.setCurrentWidget(tab)
        if step == STEP_ADD_ARTISTS:
            # 명단이 비어 있다 — 커서를 붙여넣기 상자에 놓아 바로 칠 수 있게 한다.
            self.roster_tab.add_edit.setFocus()
        return True

    def commit(self) -> None:
        """탭들의 화면 값을 `settings.arena`에 되쓰고 아레나 데이터를 저장한다."""
        for tab in self._tabs:
            tab.commit()
        self._service.save()

    def show_status(self, message: str) -> None:
        self.header.set_message(message)

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setWindowTitle(tr("arena.title"))
        self.header.retranslate()
        for index, tab in enumerate(self._tabs):
            self.tabs.setTabText(index, tr(f"arena.tab_{tab.KEY}"))
            tab.retranslate()
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("arena.close"))
        self.reset_size_button.setText(tr("arena.reset_size"))
        self.reset_size_button.setToolTip(tr("arena.reset_size_hint"))

    # ── 내부 ────────────────────────────────────────────────────────────

    def _on_state_changed(self) -> None:
        """한 탭이 작가·조합을 바꿨다 — 나머지 탭도 같은 상태를 보게 한다."""
        self.refresh()

    def _on_arena_event(self, event) -> None:
        self._update_progress(event)
        for tab in self._tabs:
            tab.on_arena_event(event)
        # 그림이 한 장 나올 때마다 `대기`가 줄어든다 — 상태줄 숫자도 따라가야 한다.
        self.header.set_status(pipeline_status(self._service.state))

    def _update_progress(self, event) -> None:
        """진행 막대는 창이 직접 움직인다 — 어느 탭에서도 보여야 하기 때문이다."""
        if isinstance(event, ArenaBatchStarted):
            self.progress.setRange(0, event.total)
            self.progress.setValue(0)
            self.progress.setVisible(True)
        elif isinstance(event, ArenaImageReady):
            self.progress.setValue(event.index)
        elif isinstance(event, ArenaBatchFinished):
            self.progress.setVisible(False)

    def _on_language_changed(self, _language: str) -> None:
        self.retranslate()

    # ── 창 크기 ─────────────────────────────────────────────────────────

    def reset_size(self) -> None:
        """창을 기본 크기로 되돌리고 화면 가운데에 놓는다.

        낮은 해상도 PC에서 창이 화면보다 커져 윗부분이 잘린다는 제보가 있었다.
        저장된 위치·크기와 월드컵 배율까지 되돌려, 무슨 값이 어긋났든 한 번에
        빠져나올 수 있게 한다.
        """
        if self.isFullScreen() or self.isMaximized():
            self.showNormal()
        self._qsettings.remove(_GEOMETRY_KEY)
        self._apply_minimum_size()
        width, height = self._default_size()
        self.resize(width, height)
        area = self._available_geometry()
        if area is not None:
            self.move(
                area.left() + max(0, (area.width() - width) // 2),
                area.top() + max(0, (area.height() - height) // 2),
            )
        self.match_tab.reset_image_scale()
        self.show_status(self._i18n.get_text("arena.size_reset"))

    def _available_geometry(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        return None if screen is None else screen.availableGeometry()

    def _default_size(self) -> tuple[int, int]:
        """기본 크기 — 화면이 좁으면 화면에 맞춘다."""
        width, height = DEFAULT_SIZE
        area = self._available_geometry()
        if area is not None:
            width = min(width, area.width())
            height = min(height, area.height())
        return width, height

    def _apply_minimum_size(self) -> None:
        """최소 크기도 화면을 넘지 않게 한다.

        최소가 화면보다 크면 창을 줄일 수도, 화면 안으로 넣을 수도 없다 —
        `ensure_on_screen`이 옮겨 봐야 아래위가 잘린 채로 남는다.
        """
        width, height = FLOOR_SIZE
        area = self._available_geometry()
        if area is not None:
            width = min(width, area.width())
            height = min(height, area.height())
        self.setMinimumSize(width, height)

    def _restore_geometry(self) -> None:
        geometry = self._qsettings.value(_GEOMETRY_KEY)
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except (TypeError, ValueError):  # 손상된 값 — 기본 크기로 뜬다
                logger.debug("bad saved geometry for the arena window")
        if self.isFullScreen():
            # 전체 화면 토글이 있던 버전에서 그 상태로 저장된 값 — 그대로 띄우면
            # 제목 표시줄 없는 창을 또 만난다.
            self.showNormal()
        # 저장된 위치가 화면 밖이면(모니터 구성이 바뀌었거나 값이 어긋났거나)
        # 제목 표시줄이 잘려 창을 옮길 수도, 닫을 수도 없다.
        ensure_on_screen(self)
        index = self._qsettings.value(_TAB_KEY, 0, type=int)
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        """창을 닫아도 생성은 계속된다 — 멈추려면 중지 버튼을 눌러야 한다.

        닫자마자 잡을 죽이면, 크레딧을 이미 쓴 그림을 버리는 셈이 된다.
        """
        self.commit()
        self.judge_tab.shutdown()  # 판독 워커가 돌고 있으면 멈추고 기다린다
        self._qsettings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        self._qsettings.setValue(_TAB_KEY, self.tabs.currentIndex())
        self._service.unsubscribe(self._on_arena_event)
        self._i18n.unsubscribe(self._on_language_changed)
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Esc로 창이 닫히는 것을 막는다 — 표를 편집하다 실수로 누르기 쉽다."""
        if event.key() == Qt.Key.Key_Escape:
            return
        super().keyPressEvent(event)


__all__ = ["ArenaDialog"]
