"""세팅별 연속 생성의 상태와 진행 판정 (Qt-free).

V4의 "세팅별 연속 생성" — 세팅 파일 여러 개를 골라, **생성 한 장마다 다음 파일을
불러오며** 순환한다. 각 파일로 만든 이미지는 `save_dir/<파일명>/`에 들어간다.

이 상태 머신은 지금까지 `MainWindow`에 다섯 개의 필드로 흩어져 있었다. 리스트 필드를
클래스 속성 기본값으로 두면 인스턴스끼리 공유되는 함정을 주석으로 막아야 했는데, 그
자체가 상태가 뷰에 눌러앉아 있다는 신호였다. 여기로 옮겨 **위젯 없이 단위 테스트**할 수
있게 한다 — 파일 선택·잡 시작·상태바 표시 같은 Qt 붙은 일은 그대로 뷰가 맡는다.

진행 프로토콜:
    start(paths, total) → [잡 1장 시작 → job_finished(...) → 계속?] 반복 → 끝
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class BatchProgress:
    """`job_finished()`가 돌려주는 판정.

    - `continuing`  — 다음 세팅 파일로 순환을 이어갈지
    - `completed`   — 순환 전체의 누적 완료 매수 (뷰가 상태바·이벤트에 쓴다)
    - `stopped`     — 사용자가 멈췄거나 잡이 중지로 끝났는지
    """

    continuing: bool
    completed: int
    stopped: bool


class SettingsBatchController:
    """세팅별 연속 생성의 상태와 진행 판정만 담는다 (Qt 없음).

    RNG를 주입할 수 있어 무작위 순서도 결정적으로 테스트한다.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self.paths: list[str] = []
        self.index: int = -1
        self.total: int = 0
        self.completed: int = 0
        self.stop_requested: bool = False

    @property
    def active(self) -> bool:
        """순환 중인가 — 파일 목록이 비어 있지 않으면 진행 중이다."""
        return bool(self.paths)

    def start(self, paths: list[str], total: int) -> None:
        """새 순환을 시작 상태로 만든다 (아직 첫 잡을 뽑지는 않는다)."""
        self.paths = list(paths)
        self.index = -1
        self.total = total
        self.completed = 0
        self.stop_requested = False

    def reset(self) -> None:
        """순환을 끝낸다 (`active`가 False가 된다)."""
        self.paths = []

    def request_stop(self) -> None:
        """이번 이미지를 끝으로 순환도 멈춘다."""
        self.stop_requested = True

    def next_index(self, *, randomize: bool) -> int:
        """다음에 쓸 세팅 파일의 자리. 기본은 고른 순서대로, 옵션을 켜면 무작위.

        무작위일 때 같은 파일이 연달아 두 번 나오지는 않게 한다 — 세팅을 여러 개
        고른 이유가 번갈아 쓰려는 것이기 때문이다 (파일이 2개면 결국 번갈아 돈다).

        이 메서드는 `self.index`도 갱신하고 그 값을 돌려준다.
        """
        total = len(self.paths)
        if not randomize or total < 2:
            self.index = (self.index + 1) % total
        else:
            choices = [i for i in range(total) if i != self.index]
            self.index = self._rng.choice(choices)
        return self.index

    def current_path(self) -> str:
        """지금 자리의 세팅 파일 경로."""
        return self.paths[self.index]

    def job_finished(self, *, error: bool, stopped: bool) -> BatchProgress:
        """한 세팅 파일의 1장 잡이 끝났다 — 순환을 이어갈지 판정한다.

        에러 없이 정상 완료했으면 누적 매수를 하나 늘리고, 총 매수(0=무제한)에
        닿지 않았으면 계속한다. 계속하지 않으면 순환을 끝낸다(`reset`).
        """
        stopped = stopped or self.stop_requested
        continuing = False
        if not error and not stopped:
            self.completed += 1
            continuing = self.total == 0 or self.completed < self.total
        if not continuing:
            self.reset()
        return BatchProgress(continuing=continuing, completed=self.completed, stopped=stopped)


__all__ = ["BatchProgress", "SettingsBatchController"]
