"""UI 멈춤 감시 — 메인 스레드가 응답하지 않으면 그 순간의 스택을 남긴다 (Qt 없음).

세그폴트는 `faulthandler`가 `naiauto.crash.log`에 스택을 남기지만, **멈춤**(Windows의
"응답 없음")은 프로세스가 죽지 않으므로 아무 흔적도 남지 않는다. 사용자가 창을 강제로
닫으면 그대로 끝이라, 어디서 멈췄는지 사후에 알 방법이 없었다 (v0.19.0 제보: i2i에서
마스크를 그린 뒤 스크롤하다 응답 없음 — 로그가 비어 원인을 확정하지 못했다).

동작:

- UI 쪽이 타이머로 `beat()`를 주기적으로 부른다 (이벤트 루프가 도는 동안만 불린다).
- 감시 스레드가 마지막 `beat()` 뒤로 `threshold`초가 지나면 `dump(사유)`를 부른다.
  아직도 멈춰 있으면 `threshold × REPEAT_FACTOR`초에 한 번 더 남긴다 — 두 스택이 같으면
  한 곳에 막힌 것이고, 다르면 오래 걸리는 작업이 계속 도는 것이다.
- 다시 `beat()`가 오면 얼마나 멈췄었는지 로그에 남기고 다음 멈춤을 기다린다.

PC가 절전에 들어갔다 깨어나면 UI와 감시 스레드가 함께 멈췄던 것이므로 멈춤으로 세지
않는다 (감시 스레드 자신의 점검 간격이 크게 벌어졌는지로 판별한다).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

#: 이 시간 동안 이벤트 루프가 돌지 않으면 멈춤으로 본다 (Windows의 "응답 없음" 기준과 같다).
HANG_THRESHOLD = 5.0
#: 감시 스레드가 점검하는 간격.
CHECK_INTERVAL = 1.0
#: UI가 `beat()`를 부르는 간격 (밀리초) — `threshold`보다 충분히 짧아야 한다.
HEARTBEAT_MS = 1000
#: 첫 덤프 뒤에도 멈춰 있으면 `threshold × REPEAT_FACTOR`초에 한 번 더 남긴다.
REPEAT_FACTOR = 4
#: 점검 간격이 이 배수 이상 벌어졌으면 감시 스레드도 멈췄던 것 — 절전 복귀로 본다.
SUSPEND_FACTOR = 5

logger = logging.getLogger(__name__)


class HangWatchdog:
    """`beat()`가 끊기면 `dump`를 부르는 감시자.

    `dump(reason)`은 감시 스레드에서 불린다. 스택을 남겼으면 True를 돌려준다.
    """

    def __init__(
        self,
        dump: Callable[[str], bool],
        *,
        threshold: float = HANG_THRESHOLD,
        interval: float = CHECK_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dump = dump
        self._threshold = threshold
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        now = clock()
        self._last_beat = now
        self._last_check = now
        #: 이번 멈춤에서 이미 남긴 덤프 수 (0 = 멈춤 아님).
        self._dumps = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── UI 스레드 ────────────────────────────────────────

    def beat(self) -> None:
        """이벤트 루프가 살아 있다는 신호."""
        with self._lock:
            now = self._clock()
            stalled = now - self._last_beat
            was_hung = self._dumps > 0
            self._last_beat = now
            self._dumps = 0
        if was_hung:
            logger.warning("UI thread responsive again after %.1fs", stalled)

    # ── 감시 스레드 ──────────────────────────────────────

    def check(self) -> bool:
        """한 번 점검한다. 이번 점검에서 스택을 남겼으면 True."""
        with self._lock:
            now = self._clock()
            gap = now - self._last_check
            self._last_check = now
            if gap >= self._interval * SUSPEND_FACTOR:
                # 감시 스레드도 돌지 못했다 (절전·디버거 정지) — 기준 시각을 새로 잡는다.
                self._last_beat = now
                return False
            stalled = now - self._last_beat
            due = self._threshold * (REPEAT_FACTOR if self._dumps else 1)
            if self._dumps >= 2 or stalled < due:
                return False
            self._dumps += 1
            first = self._dumps == 1
        reason = f"UI thread unresponsive for {stalled:.1f}s" + ("" if first else " (still stuck)")
        try:
            written = self._dump(reason)
        except Exception:  # noqa: BLE001 — 진단 기능이 앱을 죽여서는 안 된다
            logger.exception("failed to dump stacks for UI hang")
            return False
        logger.error("%s — thread stacks %s", reason, "written to crash log" if written else "not written")
        return written

    def start(self) -> None:
        """감시 스레드를 띄운다. 이미 돌고 있으면 아무것도 하지 않는다."""
        if self._thread is not None:
            return
        with self._lock:
            now = self._clock()
            self._last_beat = now
            self._last_check = now
            self._dumps = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ui-hang-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """감시를 끝낸다 — 이벤트 루프가 끝난 뒤의 정리 작업을 멈춤으로 세지 않도록."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=self._interval * 2)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.check()


__all__ = [
    "CHECK_INTERVAL",
    "HANG_THRESHOLD",
    "HEARTBEAT_MS",
    "REPEAT_FACTOR",
    "SUSPEND_FACTOR",
    "HangWatchdog",
]
