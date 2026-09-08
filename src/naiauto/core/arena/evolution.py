"""유전 알고리즘 — 검증된 조합끼리 교배해 다음 세대를 만든다 (Qt-free).

월드컵으로 점수가 매겨진 상위 조합을 부모로 삼아, 작가 구성을 섞고(교차) 일부를
바꾼 뒤(돌연변이) 자식을 낸다. 반대로 판수가 쌓였는데도 점수가 낮은 조합은
한 번에 정리한다.

원본 프로그램과 다른 점 두 가지:

- **가중치도 유전한다.** 원본은 부모의 작가 이름만 물려주고 가중치는 전부 새로
  뽑았다. 그러면 "이 작가를 1.6으로 세게 쓴 게 좋았다"는 정보가 매 세대 사라진다.
  여기서는 물려받은 가중치를 흔들기만(jitter) 한다.
- **자식은 부모 점수의 평균에서 시작한다.** 원본은 1050 고정이라 아직 아무도 안 본
  자식이 검증된 부모보다 위에 앉았다. 판수가 0이면 K가 최대(32)라 몇 판이면
  제자리를 찾는다.
"""

from __future__ import annotations

import random

from .combos import build_slots, name_signature, normalize_weight, round_weight
from .models import ArenaState, ArtistEntry, Combo, ComboGenParams, ComboSlot

#: 교배 부모로 쓸 상위 조합 수. 너무 좁으면 같은 자식만 나오고, 너무 넓으면
#: 검증되지 않은 조합이 유전자 풀을 흐린다.
DEFAULT_PARENT_POOL = 20

#: 일괄 정리 기본 조건 — 이만큼 싸웠는데 이 점수 아래면 취향이 아니라고 본다.
DEFAULT_PURGE_MIN_MATCHES = 5
DEFAULT_PURGE_ELO = 960.0


def select_parents(combos: list[Combo], top_n: int = DEFAULT_PARENT_POOL) -> list[Combo]:
    """부모 후보 — 평가를 받았거나(전적 있음) 사용자가 잠근 조합 중 상위 `top_n`.

    잠근 조합은 전적이 없어도 넣는다. "이건 무조건 남긴다"는 사용자의 명시적 의사이므로
    유전자 풀에도 들어가야 한다.
    """
    eligible = [combo for combo in combos if combo.matches > 0 or combo.locked]
    eligible.sort(key=lambda combo: combo.elo, reverse=True)
    return eligible[:top_n] if top_n > 0 else eligible


def _inherited_slots(parent_a: Combo, parent_b: Combo) -> dict[str, float]:
    """두 부모의 (작가 → 가중치). 양쪽에 다 있는 작가는 가중치 평균을 물려준다."""
    merged: dict[str, list[float]] = {}
    for parent in (parent_a, parent_b):
        for slot in parent.slots:
            merged.setdefault(slot.name, []).append(slot.weight)
    return {name: sum(weights) / len(weights) for name, weights in merged.items()}


def crossover(
    parent_a: Combo,
    parent_b: Combo,
    params: ComboGenParams,
    pool: list[ArtistEntry] | None = None,
    rng: random.Random | None = None,
) -> tuple[ComboSlot, ...]:
    """두 부모의 작가를 섞어 자식의 자리를 만든다 (균등 교차 + 돌연변이).

    자리 수는 설정한 범위에서 다시 뽑는다. 부모가 가진 작가만으로 그 수를 못 채우면
    명단에서 빌려 온다 — 세대가 갈수록 유전자 풀이 말라붙는 것을 막는다.
    """
    rng = rng or random
    params = params.clamped()
    pool = pool or []
    by_name = {entry.name: entry for entry in pool}

    inherited = _inherited_slots(parent_a, parent_b)
    names = list(inherited)
    rng.shuffle(names)

    target = rng.randint(params.min_artists, params.max_artists)
    chosen = names[:target]

    if len(chosen) < target and pool:
        spare = [entry.name for entry in pool if entry.name not in inherited]
        rng.shuffle(spare)
        chosen.extend(spare[: target - len(chosen)])

    if not chosen:  # 부모도 명단도 비었다 — 만들 수 있는 게 없다
        return ()

    low, high = normalize_weight(params.weight_min), normalize_weight(params.weight_max)
    slots: list[ComboSlot] = []
    used: set[str] = set()
    for name in chosen:
        # 돌연변이: 이 자리를 명단의 다른 작가로 바꾼다. 이미 이 자식에 들어간
        # 작가는 후보에서 빼야 한다 — 그러지 않으면 뽑을 때마다 부딪혀서 돌연변이가
        # 조용히 취소되고(부모 유전자 그대로), 확률을 올려도 다양성이 안 늘어난다.
        if pool and rng.random() < params.mutation_rate:
            fresh = [entry.name for entry in pool if entry.name not in used]
            if fresh:
                name = rng.choice(fresh)
        if name in used:
            continue
        used.add(name)

        entry = by_name.get(name)
        if entry is not None and entry.locked_weight is not None:
            # 고정한 가중치는 돌연변이도, 범위도, 증감 폭도 건드리지 않는다
            slots.append(ComboSlot(name=name, weight=normalize_weight(entry.locked_weight)))
            continue
        # 물려받은 값을 **먼저** 범위 안으로 접고 나서 흔든다. 반대로 하면, 범위를
        # 좁혔을 때 범위 밖의 부모 값들이 전부 경계로 눌려 자식이 죄다 같은 가중치가
        # 된다 (0.8~1.8로 만든 부모를 0.5~0.8로 교배하면 전부 0.8이 됐다).
        base = min(high, max(low, inherited.get(name, (low + high) / 2)))
        jitter = rng.uniform(-params.weight_jitter, params.weight_jitter)
        weight = max(low, min(high, base + jitter))
        slots.append(ComboSlot(name=name, weight=round_weight(weight, params.weight_step)))
    return tuple(slots)


def breed(
    state: ArenaState,
    params: ComboGenParams,
    count: int,
    top_n: int = DEFAULT_PARENT_POOL,
    rng: random.Random | None = None,
) -> list[Combo]:
    """상위 조합을 교배해 자식 `count`개를 만든다.

    이미 있는 조합과 작가 구성이 같은 자식은 버린다 — 같은 그림을 또 뽑는 데
    크레딧을 쓸 이유가 없다. 그래서 요청보다 적게 나올 수 있다.
    부모가 2개 미만이면 빈 목록.
    """
    rng = rng or random
    params = params.clamped()
    parents = select_parents(state.combos, top_n)
    if len(parents) < 2 or count <= 0:
        return []

    seen = {name_signature(combo.slots) for combo in state.combos}
    children: list[Combo] = []
    attempts = 0
    max_attempts = max(count * 20, 50)
    while len(children) < count and attempts < max_attempts:
        attempts += 1
        parent_a, parent_b = rng.sample(parents, 2)
        slots = crossover(parent_a, parent_b, params, state.artists, rng)
        if not slots:
            break  # 명단이 비었다 — 더 시도해도 같다
        signature = name_signature(slots)
        if signature in seen:
            continue
        seen.add(signature)
        children.append(
            Combo(
                slots=slots,
                elo=(parent_a.elo + parent_b.elo) / 2,
                generation=max(parent_a.generation, parent_b.generation) + 1,
                parents=(parent_a.id, parent_b.id),
            )
        )
    return children


def purge_candidates(
    combos: list[Combo],
    min_matches: int = DEFAULT_PURGE_MIN_MATCHES,
    elo_threshold: float = DEFAULT_PURGE_ELO,
) -> list[Combo]:
    """정리 대상 — 충분히 싸웠는데 점수가 기준 아래인 조합.

    잠근 조합과 즐겨찾기는 제외한다. 삭제는 되돌릴 수 없으므로 UI는 이 목록을
    먼저 보여 주고 확인을 받는다.
    """
    return [
        combo
        for combo in combos
        if not combo.locked
        and not combo.favorite
        and combo.matches >= min_matches
        and combo.elo < elo_threshold
    ]


def combos_with_artist(combos: list[Combo], name: str) -> list[Combo]:
    """특정 작가가 낀 조합 (잠금·즐겨찾기 제외).

    "이 작가는 내 취향이 아니다"를 알게 됐을 때 관련 조합을 한 번에 치우는 용도다.
    """
    target = name.strip().casefold()
    return [
        combo
        for combo in combos
        if not combo.locked
        and not combo.favorite
        and any(slot.name.casefold() == target for slot in combo.slots)
    ]


def regenerate_weights(
    combo: Combo,
    params: ComboGenParams,
    pool: list[ArtistEntry] | None = None,
    rng: random.Random | None = None,
) -> tuple[ComboSlot, ...]:
    """작가는 그대로 두고 가중치만 새로 뽑는다 (월드컵의 '세팅 리롤').

    같은 작가 조합이라도 세기 배분이 달라지면 그림이 꽤 달라진다.
    """
    by_name = {entry.name: entry for entry in (pool or [])}
    return build_slots([slot.name for slot in combo.slots], params, by_name, rng)
