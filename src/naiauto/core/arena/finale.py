"""통계 결산 — 위에서부터 몇 등까지, 조합마다 몇 장씩 (Qt-free).

월드컵을 돌려 순위가 나오면 다음에 하고 싶은 일은 정해져 있다: **위에서부터
차례로 실제 그림을 뽑아 한 벌로 보는 것**. 조합을 하나씩 골라 프롬프트에 붙이고
누르기를 반복하는 대신, 순위대로 큐를 만들어 한 번에 돌린다.

여기 있는 것은 "무엇을 몇 장 뽑을지"를 정하는 계산뿐이다. 실제 요청 만들기는
`services/arena_service.build_finale_provider`가, 생성은 메인 창의 기존 생성
파이프라인이 한다 — 그래서 결과가 **결과 폴더**에 그대로 쌓이고 갤러리에도 뜬다
(아레나가 뽑는 습작과는 다른 자리다).
"""

from __future__ import annotations

from .elo import leaderboard
from .models import ArenaState, Combo

#: 결산에 올릴 기본 등수와 조합당 장수.
DEFAULT_TOP_N = 10
DEFAULT_PER_COMBO = 1

#: 화면에서 고를 수 있는 상한. 크레딧을 쓰는 일이라 무제한으로 두지 않는다.
MAX_TOP_N = 200
MAX_PER_COMBO = 20


def finale_combos(state: ArenaState, top_n: int, *, include_unrated: bool = False) -> list[Combo]:
    """결산에 올릴 조합 — 통계 표와 **같은 순서**로 위에서부터 `top_n`개.

    기본적으로 한 번도 안 싸운 조합은 뺀다. 1000점은 실력이 아니라 '모름'이라,
    결산에 섞이면 순위가 있는 것처럼 보인다. `include_unrated=True`면 전부 올린다
    (아직 월드컵을 안 돌렸는데 그냥 다 뽑아 보고 싶은 경우).
    """
    combos = state.combos if include_unrated else [c for c in state.combos if c.matches > 0]
    return leaderboard(combos, max(0, top_n))


def finale_total(combos: list[Combo], per_combo: int) -> int:
    """실제로 뽑을 장수. 화면에 미리 보여 줘야 하는 값이다 — 크레딧이 나간다."""
    return len(combos) * max(1, per_combo)


def combo_for_index(combos: list[Combo], per_combo: int, index: int) -> Combo | None:
    """`index`번째 장(1부터)이 어느 조합의 것인지.

    1등을 `per_combo`장 뽑고 2등으로 넘어간다 — 등수 순서 그대로 쌓이도록.
    """
    if not combos or index < 1:
        return None
    position = (index - 1) // max(1, per_combo)
    return combos[position] if position < len(combos) else None


__all__ = [
    "DEFAULT_PER_COMBO",
    "DEFAULT_TOP_N",
    "MAX_PER_COMBO",
    "MAX_TOP_N",
    "combo_for_index",
    "finale_combos",
    "finale_total",
]
