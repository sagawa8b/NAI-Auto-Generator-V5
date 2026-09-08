"""Elo 점수·티어·대진 짜기 (Qt-free).

원본 프로그램은 K=32 고정에 상대를 완전 무작위로 골랐다. 그러면 초반 몇 판의
운이 끝까지 남고, 실력차가 큰 대진이 반복되어 클릭 수 대비 얻는 정보가 적다.
여기서는 두 가지를 바꿨다.

1. **적응형 K** — 판수가 쌓일수록 점수가 덜 흔들린다. 티어 "확정" 판정의 근거다.
2. **스위스식 대진** — 덜 싸운 조합을 먼저 올리고, 그 상대는 점수가 비슷한
   쪽에서 고른다. 승부가 뻔한 대결이 줄어 같은 클릭 수로 순위가 더 정확해진다.

조합이 대결하면 그 조합에 들어 있던 작가들도 점수를 나눠 받는다. 원본은 승패에
±15 고정이었는데, 여기서는 조합의 점수 변화량을 **자리 가중치에 비례해** 나눈다 —
1.8로 들어간 작가가 0.8로 들어간 작가보다 결과에 더 책임이 있다는 뜻이다.
"""

from __future__ import annotations

import random

from .models import DEFAULT_ELO, ArenaState, Combo, ComboSlot

RESULT_A = "a"  # A 승
RESULT_B = "b"  # B 승
RESULT_DRAW = "draw"  # 무승부
RESULT_BOTH_WIN = "both_win"  # 둘 다 마음에 듦 — 둘 다 상대 수준을 이긴 것으로 친다
RESULT_BOTH_LOSE = "both_lose"  # 둘 다 아쉬움 — 둘 다 상대 수준에 진 것으로 친다
RESULTS = (RESULT_A, RESULT_B, RESULT_DRAW, RESULT_BOTH_WIN, RESULT_BOTH_LOSE)

#: 판수 구간별 K값. 위에서부터 처음 맞는 구간을 쓴다.
#: 초반에는 크게 움직여 빨리 제자리를 찾고, 판수가 쌓이면 잘 안 흔들린다.
K_SCHEDULE = ((5, 32.0), (10, 24.0))
K_SETTLED = 16.0

#: 이만큼 싸우면 티어가 "확정"된 것으로 본다 (K가 최소로 떨어지는 지점과 같다).
SETTLED_MATCHES = 10

#: 티어 경계 — (티어 이름, 하한). 위에서부터 처음 넘는 구간이 그 조합의 티어다.
TIER_THRESHOLDS = (
    ("S", 1200.0),
    ("A", 1120.0),
    ("B", 1040.0),
    ("C", 960.0),
    ("D", 880.0),
)
LOWEST_TIER = "F"
TIERS = tuple(name for name, _ in TIER_THRESHOLDS) + (LOWEST_TIER,)

#: 대진을 짤 때 "점수가 비슷한" 상대를 몇 명까지 후보로 볼지.
#: 1이면 항상 같은 짝만 붙고, 너무 크면 무작위와 다를 게 없다.
_OPPONENT_CANDIDATES = 5


def k_factor(matches: int) -> float:
    """치른 판수에 따른 K값."""
    for limit, value in K_SCHEDULE:
        if matches < limit:
            return value
    return K_SETTLED


def expected_score(elo: float, opponent_elo: float) -> float:
    """`elo`가 `opponent_elo`를 이길 확률 (표준 Elo 기대승률)."""
    return 1.0 / (1.0 + 10.0 ** ((opponent_elo - elo) / 400.0))


def tier_of(elo: float) -> str:
    """점수를 S~F 티어로. 판수는 보지 않는다 — 확정 여부는 `is_settled()`가 답한다."""
    for name, threshold in TIER_THRESHOLDS:
        if elo >= threshold:
            return name
    return LOWEST_TIER


def is_settled(matches: int) -> bool:
    """이 조합의 티어를 믿어도 되는지 (K가 최소로 떨어졌는지)."""
    return matches >= SETTLED_MATCHES


def unsettled(combos: list[Combo]) -> list[Combo]:
    """아직 티어가 확정되지 않은, 그림이 있는 조합들.

    "지금쯤 다 확정됐으려나?" 하고 탭을 들락거리지 않아도 되게 UI가 이 수를 띄운다.
    """
    return [combo for combo in combos if combo.has_image and not is_settled(combo.matches)]


def _scores(result: str) -> tuple[float, float]:
    """결과를 (A의 점수, B의 점수)로. 둘 다 승/패는 상대를 기준선으로 삼는다.

    `both_win`은 "둘 다 상대만큼의 조합을 이겼다"는 뜻이라 양쪽 모두 1점을 받는다.
    서로에게서 점수를 뺏는 게 아니라 각자 상대의 현재 실력을 기준으로 오르내리므로,
    강한 상대와 붙었을 때 더 많이 오른다.
    """
    if result == RESULT_A:
        return 1.0, 0.0
    if result == RESULT_B:
        return 0.0, 1.0
    if result == RESULT_BOTH_WIN:
        return 1.0, 1.0
    if result == RESULT_BOTH_LOSE:
        return 0.0, 0.0
    return 0.5, 0.5  # RESULT_DRAW 및 알 수 없는 값


def rate_pair(
    elo_a: float,
    matches_a: int,
    elo_b: float,
    matches_b: int,
    result: str,
) -> tuple[float, float]:
    """대결 뒤의 (A 점수, B 점수)를 돌려준다. 상태를 바꾸지 않는 순수 함수."""
    score_a, score_b = _scores(result)
    new_a = elo_a + k_factor(matches_a) * (score_a - expected_score(elo_a, elo_b))
    new_b = elo_b + k_factor(matches_b) * (score_b - expected_score(elo_b, elo_a))
    return new_a, new_b


def weight_shares(slots: tuple[ComboSlot, ...]) -> list[float]:
    """자리별 책임 비율 (합이 1.0).

    음수 가중치는 0으로 본다 (프롬프트에서 빼는 방향이라 '기여'가 아니다).
    전부 0이면 똑같이 나눈다 — 0으로 나누는 일이 없어야 한다.
    """
    if not slots:
        return []
    positive = [max(0.0, slot.weight) for slot in slots]
    total = sum(positive)
    if total <= 0:
        return [1.0 / len(slots)] * len(slots)
    return [value / total for value in positive]


def propagate_to_artists(state: ArenaState, combo: Combo, delta: float, won: bool) -> None:
    """조합의 점수 변화를 그 조합에 낀 작가들에게 가중치 비례로 나눠 준다.

    명단에서 지운 작가가 낀 옛 조합도 대결에 나올 수 있으므로, 명단에 없는 이름은
    조용히 건너뛴다.
    """
    shares = weight_shares(combo.slots)
    for slot, share in zip(combo.slots, shares, strict=True):
        entry = state.artist(slot.name)
        if entry is None:
            continue
        entry.elo += delta * share
        entry.matches += 1
        if won:
            entry.wins += 1


def apply_match(state: ArenaState, combo_a: Combo, combo_b: Combo, result: str) -> None:
    """대결 결과를 상태에 반영한다 — 조합 점수, 작가 점수, 전적, 총 대결 수.

    `result`가 아는 값이 아니면 무승부로 처리한다 (UI가 새 버튼을 붙였는데 여기에
    반영이 안 된 경우, 조용히 아무 일도 안 일어나는 것보다 낫다).
    """
    if combo_a.id == combo_b.id:  # 자기 자신과의 대결은 점수에 의미가 없다
        return

    before_a, before_b = combo_a.elo, combo_b.elo
    combo_a.elo, combo_b.elo = rate_pair(combo_a.elo, combo_a.matches, combo_b.elo, combo_b.matches, result)
    score_a, score_b = _scores(result)
    combo_a.matches += 1
    combo_b.matches += 1
    combo_a.wins += int(score_a >= 1.0)
    combo_b.wins += int(score_b >= 1.0)
    state.total_matches += 1

    propagate_to_artists(state, combo_a, combo_a.elo - before_a, score_a >= 1.0)
    propagate_to_artists(state, combo_b, combo_b.elo - before_b, score_b >= 1.0)


def pick_match(
    combos: list[Combo],
    rng: random.Random | None = None,
    avoid: tuple[str, str] | None = None,
) -> tuple[Combo, Combo] | None:
    """다음 대진을 고른다. 그림이 없는 조합은 나가지 못한다.

    덜 싸운 조합을 먼저 올리고(모두가 비슷한 판수를 갖게 된다), 그 상대는 점수가
    가까운 후보 중에서 역시 덜 싸운 쪽을 고른다. `avoid`로 직전 대진을 넘기면
    같은 짝이 연달아 나오는 것을 피한다 (대안이 없으면 그냥 다시 붙인다).

    후보가 2개 미만이면 None.
    """
    rng = rng or random
    eligible = [combo for combo in combos if combo.has_image]
    if len(eligible) < 2:
        return None

    fewest = min(combo.matches for combo in eligible)
    first_pool = [combo for combo in eligible if combo.matches == fewest]
    first = rng.choice(first_pool)

    others = [combo for combo in eligible if combo.id != first.id]
    others.sort(key=lambda combo: (abs(combo.elo - first.elo), combo.matches))
    candidates = others[:_OPPONENT_CANDIDATES]

    if avoid is not None and len(candidates) > 1:
        pair = {first.id}
        fresh = [combo for combo in candidates if pair | {combo.id} != set(avoid)]
        if fresh:
            candidates = fresh

    # 후보 안에서는 덜 싸운 쪽을 먼저, 그 안에서 점수가 더 가까운 쪽을 고른다.
    # 완전히 같은 조건이 여럿이면 그때만 무작위로 — 같은 짝이 굳어지지 않게.
    best = min((combo.matches, abs(combo.elo - first.elo)) for combo in candidates)
    second = rng.choice(
        [combo for combo in candidates if (combo.matches, abs(combo.elo - first.elo)) == best]
    )

    # 좌우 배치는 무작위로 — 늘 같은 쪽에 강자가 오면 클릭 습관이 결과를 물들인다.
    return (first, second) if rng.random() < 0.5 else (second, first)


def leaderboard(combos: list[Combo], limit: int = 0) -> list[Combo]:
    """점수 높은 순. 대결 기록이 없는 조합은 뒤로 민다 (1000점은 '모름'이지 실력이 아니다)."""
    ranked = sorted(combos, key=lambda combo: (combo.matches > 0, combo.elo), reverse=True)
    return ranked[:limit] if limit > 0 else ranked


def artist_ranking(state: ArenaState, limit: int = 0) -> list:
    """작가 순위 — 대결 기록이 있는 작가가 위로."""
    ranked = sorted(state.artists, key=lambda entry: (entry.matches > 0, entry.elo), reverse=True)
    return ranked[:limit] if limit > 0 else ranked


def tier_counts(combos: list[Combo]) -> dict[str, int]:
    """티어별 조합 수 — 통계 탭이 쓴다. 모든 티어 키가 항상 들어 있다."""
    counts = dict.fromkeys(TIERS, 0)
    for combo in combos:
        counts[tier_of(combo.elo)] += 1
    return counts


def mean_elo(combos: list[Combo]) -> float:
    """대결 기록이 있는 조합의 평균 점수 (없으면 기본값)."""
    rated = [combo.elo for combo in combos if combo.matches > 0]
    return sum(rated) / len(rated) if rated else DEFAULT_ELO
