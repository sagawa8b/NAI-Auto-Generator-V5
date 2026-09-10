"""파이프라인 안내 — 지금 어디까지 왔고, 다음에 무엇을 할 차례인지.

아레나는 `작가 → 조합 → 그림 → 대결 → 교배 → 다시 대결`로 도는 루프인데, 화면은
탭 여섯 개가 평평하게 놓여 있어 "지금 뭘 해야 하지"를 사용자가 스스로 추론해야 했다.
그 추론을 여기서 **한 번만** 해 두고, 창 상단 헤더가 그대로 보여 준다.

Qt를 모르는 순수 함수다 — 규칙이 맞는지 위젯 없이 검증한다 (`core/`에는 Qt를 들이지
않는다는 저장소 규칙이기도 하다).

다음 단계를 고르는 순서는 **막히는 곳이 먼저**다: 작가가 없으면 조합을 만들 수 없고,
조합이 없으면 그림을 뽑을 수 없고, 그림이 둘 미만이면 겨룰 수 없다. 앞이 막혀 있는데
뒷 단계를 권하면 눌러 봐야 아무 일도 일어나지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .elo import unsettled
from .models import ArenaState

#: 다음에 할 일. UI는 이 값으로 라벨을 고르고, 어느 탭으로 갈지 정한다.
STEP_ADD_ARTISTS = "add_artists"  # 명단이 비었다
STEP_MAKE_COMBOS = "make_combos"  # 조합이 없거나, 겨룰 만큼 모이지 않았다
STEP_GENERATE = "generate"  # 그림을 기다리는 조합이 있다
STEP_MATCH = "match"  # 아직 티어가 확정되지 않은 조합이 있다
STEP_EVOLVE = "evolve"  # 전부 확정됐다 — 상위끼리 교배할 때

STEPS = (STEP_ADD_ARTISTS, STEP_MAKE_COMBOS, STEP_GENERATE, STEP_MATCH, STEP_EVOLVE)

#: 대결을 붙이려면 그림이 있는 조합이 최소 이만큼 있어야 한다.
MIN_COMBOS_TO_MATCH = 2


@dataclass(frozen=True)
class PipelineStatus:
    """헤더 한 줄에 필요한 숫자 전부 + 다음 할 일."""

    artists: int
    combos: int
    ready: int  # 그림이 있는 조합
    pending: int  # 그림을 기다리는 조합
    matches: int  # 지금까지 치른 대결 수
    unsettled: int  # 그림은 있으나 티어가 확정되지 않은 조합

    step: str
    #: 다음 할 일의 대상 개수 (뽑을 그림 수 · 미확정 수). 셀 것이 없으면 0.
    step_count: int


def pipeline_status(state: ArenaState) -> PipelineStatus:
    """상태를 훑어 숫자와 다음 할 일을 낸다."""
    combos = state.combos
    ready = sum(1 for combo in combos if combo.has_image)
    pending = len(combos) - ready
    left = len(unsettled(combos))

    step, count = _next_step(
        artists=len(state.artists),
        combos=len(combos),
        ready=ready,
        pending=pending,
        left=left,
    )
    return PipelineStatus(
        artists=len(state.artists),
        combos=len(combos),
        ready=ready,
        pending=pending,
        matches=state.total_matches,
        unsettled=left,
        step=step,
        step_count=count,
    )


def _next_step(*, artists: int, combos: int, ready: int, pending: int, left: int) -> tuple[str, int]:
    if artists == 0:
        return STEP_ADD_ARTISTS, 0
    if combos == 0:
        return STEP_MAKE_COMBOS, 0
    if pending:
        return STEP_GENERATE, pending
    if ready < MIN_COMBOS_TO_MATCH:
        # 그림은 다 뽑혔는데 겨룰 상대가 없다 — 조합을 더 만들어야 한다.
        return STEP_MAKE_COMBOS, 0
    if left:
        return STEP_MATCH, left
    return STEP_EVOLVE, 0


__all__ = [
    "MIN_COMBOS_TO_MATCH",
    "STEPS",
    "STEP_ADD_ARTISTS",
    "STEP_EVOLVE",
    "STEP_GENERATE",
    "STEP_MAKE_COMBOS",
    "STEP_MATCH",
    "PipelineStatus",
    "pipeline_status",
]
