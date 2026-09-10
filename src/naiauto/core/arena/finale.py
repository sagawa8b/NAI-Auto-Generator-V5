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


def finale_combos(
    state: ArenaState,
    top_n: int,
    *,
    include_unrated: bool = False,
    by_judge: bool = False,
) -> list[Combo]:
    """결산에 올릴 조합 — 통계 표와 **같은 순서**로 위에서부터 `top_n`개.

    `by_judge=False`(기본)면 사람 Elo 순위를 쓴다. 한 번도 안 싸운 조합은 뺀다 —
    1000점은 실력이 아니라 '모름'이라, 결산에 섞이면 순위가 있는 것처럼 보인다.
    `include_unrated=True`면 전부 올린다 (아직 월드컵을 안 돌렸는데 그냥 다 뽑아 보고
    싶은 경우).

    `by_judge=True`면 **LLM 판독 점수**(`judge_score`) 순위를 쓴다. 사람 Elo와 무관하게,
    LLM이 참조 그림체와 닮았다고 본 순서로 뽑는다. 점수가 없는(-1) 조합은 뺀다 —
    판독하지 않은 것을 결산에 섞으면 안 되기 때문이다. `include_unrated`는 이 경우
    무시한다 (LLM 결산의 '평가 안 됨'은 곧 '점수 없음'이라 이미 걸러진다).
    """
    if by_judge:
        return judge_leaderboard_combos(state.combos, max(0, top_n))
    combos = state.combos if include_unrated else [c for c in state.combos if c.matches > 0]
    return leaderboard(combos, max(0, top_n))


def judge_leaderboard_combos(combos: list[Combo], limit: int = 0) -> list[Combo]:
    """LLM 판독 점수 높은 순. 점수가 없는(-1) 조합은 뺀다.

    통계 탭의 `LLM 점수순` 정렬과 LLM 결산이 같은 순서를 쓰도록 한 곳에 둔다.
    services 계층의 `judge_leaderboard`와 결과는 같지만, core는 순수해야 하므로
    여기서 다시 정의한다 (service를 import하면 계층이 뒤집힌다).
    """
    ranked = sorted(
        (combo for combo in combos if combo.has_judge_score),
        key=lambda combo: combo.judge_score,
        reverse=True,
    )
    return ranked[:limit] if limit > 0 else ranked


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
    "judge_leaderboard_combos",
]
