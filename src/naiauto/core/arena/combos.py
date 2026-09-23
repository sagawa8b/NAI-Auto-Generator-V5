"""조합 만들기와 프롬프트 문자열 변환 (Qt-free).

NovelAI 가중치 문법(`1.2::artist:name::`)을 다루는 부분은 V4의 `artist_group.py`에서
가져왔다 — 숫자로 끝나는 태그의 공백 처리처럼 실제로 물렸던 함정이 들어 있다.
새로 들어간 것은 조합 자체를 만드는 `generate_combos()`와, 같은 조합이 두 번
나오지 않게 하는 서명(signature)이다.
"""

from __future__ import annotations

import random
import re

from .models import (
    ARTIST_PLACEHOLDER,
    CURVE_ASCENDING,
    CURVE_DESCENDING,
    CURVE_PEAK,
    CURVE_VALLEY,
    INSERT_PLACEHOLDER,
    INSERT_SUFFIX,
    WEIGHT_MODE_BALANCED,
    WEIGHT_MODE_CURVE,
    WEIGHT_STEP,
    ArtistEntry,
    Combo,
    ComboGenParams,
    ComboSlot,
)

_ARTIST_PREFIX = "artist:"

#: 가중치 블록 `1.2::내용::` (음수 허용, 내용에 `::`는 못 들어간다)
_BLOCK_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*::((?:(?!::).)*?)::", re.DOTALL)

#: 붙여넣기에서 작가 태그만 골라낼 때 — `artist:이름` (쉼표/`::`/줄바꿈/강조 괄호에서 끊긴다)
#: V3·V4·V4.5 프롬프트는 강조 문법 `{artist:이름}`·`[artist:이름]`을 쓴다. `}`·`]`를
#: 끊는 문자에 넣지 않으면 이름 끝에 닫는 괄호가 붙어(`aaa}`) 명단에 그대로 들어간다.
_ARTIST_TAG_RE = re.compile(r"artist:\s*([^,:\n\r{}\[\]]+)")

#: 이름을 감싸는 강조 괄호 — NovelAI 가중치 문법(강조 `{}`, 약화 `[]`). 이름 안의
#: 괄호는 `()`뿐(`meiro (yuu)`)이라 `{}`·`[]`만 벗겨도 실제 작가명은 다치지 않는다.
_EMPHASIS_CHARS = "{}[]"


def round_weight(value: float, step: float = WEIGHT_STEP) -> float:
    """가중치를 `step`의 배수로 맞추고 부동소수점 잔차를 지운다.

    조합을 **만들 때만** 쓴다. 이미 만들어진 값을 다시 이 함수에 통과시키면 안 된다 —
    다른 폭으로 만든 예전 조합(0.01 단위의 1.17)이 지금 폭(0.05)에 맞춰 1.15로
    바뀌어 버린다. 표시·파싱에는 `normalize_weight()`를 쓴다.
    """
    if step <= 0:  # 손으로 고친 설정 파일이 0을 넣으면 0으로 나누게 된다
        step = WEIGHT_STEP
    return round(round(value / step) * step, 2)


def normalize_weight(value: float) -> float:
    """표시·비교용 정규화 — 소수점 두 자리까지만 남긴다.

    증감 폭이 얼마든 두 자리면 표현할 수 있고(허용 폭이 0.01 이상), 사용자가 정한
    값을 임의의 격자에 다시 맞추지 않는다.
    """
    return round(value, 2)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def format_weight(weight: float) -> str:
    """표시용 문자열. 꼬리 0을 지운다 (1.20 → "1.2")."""
    text = f"{weight:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def fix_trailing_digit(tag: str) -> str:
    """숫자로 끝나는 태그는 닫는 `::` 앞에 공백이 필요하다.

    공백 없이 `1.5::artist:matrix16::`이 되면 NovelAI가 `16::`을 가중치 문법으로
    잘못 읽는다. V4에서 실제로 물렸던 문제라 그대로 가져왔다.

    지금은 `format_artist_block()`이 닫는 `::` 앞에 늘 공백을 넣으므로(NovelAI 공식
    문서의 표기 방식) 이 함수의 역할은 대부분 겹친다. 다른 곳에서 태그 하나를 닫을
    때를 대비해 남겨 둔다.
    """
    return tag + " " if tag and tag[-1].isdigit() else tag


def normalize_artist_name(raw: str) -> str:
    """입력 한 줄을 작가 이름으로 정리한다.

    `artist:` 접두사와 가중치 껍데기(`1.2::`, 끝의 `::`)를 벗기고 공백을 정리한다.
    이름 안의 언더스코어는 **건드리지 않는다** — 실제로 `_`가 들어간 작가명이
    있어서, 바꾸는 것은 사용자가 명시적으로 누르는 별도 기능(`underscores_to_spaces`)이다.
    """
    text = raw.strip().strip(",").strip()
    # 강조 괄호(`{`·`[`·`}`·`]`)를 양끝에서 벗긴다 — 중첩(`{{artist:aaa}}`)도 처리한다.
    # 가중치 껍데기·접두사보다 먼저 바깥 껍질을 벗겨 `{1.5::artist:aaa::}` 같은 형태도 다룬다.
    text = text.strip(_EMPHASIS_CHARS).strip()
    text = re.sub(r"^-?\d+(?:\.\d+)?\s*::", "", text).strip()
    text = text.rstrip(":").strip()
    if text.lower().startswith(_ARTIST_PREFIX):
        text = text[len(_ARTIST_PREFIX) :].strip()
    # 접두사·가중치를 벗겨 낸 뒤에도 남은 강조 괄호를 정리한다 (`{artist:aaa}` → `aaa`).
    text = text.strip(_EMPHASIS_CHARS).strip()
    return re.sub(r"\s+", " ", text)


def underscores_to_spaces(name: str) -> str:
    """`kantoku_(artist)` → `kantoku (artist)`. 명단 정리 버튼이 쓴다."""
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def extract_artist_names(text: str, require_prefix: bool = False) -> list[str]:
    """붙여넣은 덩어리에서 작가 이름을 뽑아낸다 (순서 유지, 중복 제거).

    `artist:`가 하나라도 있으면 그 태그만 골라낸다 — 가중치가 붙은 완성 프롬프트를
    통째로 붙여 넣어도 작가만 남는다. 하나도 없으면 줄/쉼표로 끊어 전부 이름으로 본다
    (작가 목록만 적어 둔 파일을 붙여 넣는 경우).

    `require_prefix=True`면 그 되돌림을 끈다. **이미지 메타데이터에서 뽑을 때** 꼭
    필요하다 — 거기 든 것은 완성 프롬프트라, `artist:`가 하나도 없으면 `1girl`이나
    `solo` 같은 태그가 전부 작가로 둔갑한다.
    """
    tagged = _ARTIST_TAG_RE.findall(text)
    if require_prefix:
        raw_names = tagged
    else:
        raw_names = tagged if tagged else re.split(r"[\n,]+", text)
    names: list[str] = []
    seen: set[str] = set()
    for raw in raw_names:
        name = normalize_artist_name(raw)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def format_artist_block(slots: tuple[ComboSlot, ...] | list[ComboSlot], use_prefix: bool = True) -> str:
    """조합을 NovelAI 가중치 프롬프트로 바꾼다.

    - 자리 순서를 지키면서 **연속된 같은 가중치**를 한 블록으로 묶는다
      (`0.8::artist:a, artist:b ::`) — 프롬프트가 짧아지고 읽기 쉬워진다.
    - 가중치 1.0은 래퍼 없이 태그만 낸다. 굳이 `1.0::...::`로 감싸면 의도치 않은
      강조가 섞인다는 지적이 있었다.
    - 닫는 `::` 앞에는 늘 공백을 하나 둔다 — NovelAI 공식 문서가 `1.5::rain, night ::`,
      `0.5::coat ::`처럼 쓰는 방식이다. 이름이 숫자로 끝날 때(`matrix16`) `16::`을
      가중치로 오독하는 것도 이 공백이 함께 막는다.
    """
    valid = [slot for slot in slots if slot.name.strip()]
    if not valid:
        return ""

    groups: list[tuple[float, list[str]]] = []
    for slot in valid:
        weight = normalize_weight(slot.weight)
        if groups and groups[-1][0] == weight:
            groups[-1][1].append(slot.name.strip())
        else:
            groups.append((weight, [slot.name.strip()]))

    parts: list[str] = []
    for weight, names in groups:
        tags = [f"{_ARTIST_PREFIX}{name}" if use_prefix else name for name in names]
        if weight == 1.0:
            parts.extend(tags)
        else:
            # 닫는 `::` 앞에 공백 하나 — NovelAI 문서의 표기(`0.8::artist:a ::`).
            joined = ", ".join(tags)
            parts.append(f"{format_weight(weight)}::{joined} ::")
    return ", ".join(parts)


def parse_artist_block(text: str, bare_weight: float = 1.0) -> tuple[list[ComboSlot], list[str]]:
    """`format_artist_block()`의 역방향 — 프롬프트 문자열을 자리 목록으로.

    가중치 블록과 맨 태그가 섞여 있어도 처리한다. 블록 밖의 태그는 가중치
    `bare_weight`(기본 1.0)로 본다 — 직접 적은 `1.0::`과 구별된다. 읽지 못한 조각은
    두 번째 값(경고 목록)으로 돌려준다 — 조용히 버리면 사용자는 태그가 왜 사라졌는지
    알 수 없다.
    """
    slots: list[ComboSlot] = []
    warnings: list[str] = []

    def add(chunk: str, weight: float) -> None:
        for raw in chunk.split(","):
            name = normalize_artist_name(raw)
            if name:
                slots.append(ComboSlot(name=name, weight=weight))

    pos = 0
    for match in _BLOCK_RE.finditer(text):
        add(text[pos : match.start()], bare_weight)
        try:
            weight = normalize_weight(float(match.group(1)))
        except ValueError:  # 정규식이 통과시킨 형태라 실질적으로 오지 않는다
            warnings.append(match.group(0))
            pos = match.end()
            continue
        add(match.group(2), weight)
        pos = match.end()

    remainder = text[pos:]
    if "::" in remainder:  # 닫히지 않은 블록 — 통째로 버리지 말고 알린다
        warnings.append(remainder.strip(" ,\t\n"))
    else:
        add(remainder, bare_weight)

    return slots, warnings


def generate_weights(
    mode: str,
    count: int,
    weight_min: float,
    weight_max: float,
    curve: str = CURVE_DESCENDING,
    rng: random.Random | None = None,
    step: float = WEIGHT_STEP,
) -> list[float]:
    """자리 수만큼 가중치를 만든다.

    - `random`: [min, max] 균등 난수 (원본 프로그램과 같은 방식)
    - `balanced`: 높음/중간/낮음 구간으로 나눠 배분 — 상위 약 25%가 그림체의 중심을
      잡고 나머지가 보조로 깔린다. 전 자리 균등 난수보다 "주작가 + 보조" 구성이
      잘 나와서 기본값으로 쓴다.
    - `curve`: 자리 순서에 대한 프리셋 곡선 (하강/상승/역V/V/평탄)
    """
    if count <= 0:
        return []
    rng = rng or random
    wmin, wmax = min(weight_min, weight_max), max(weight_min, weight_max)
    span = wmax - wmin

    if mode == WEIGHT_MODE_BALANCED:
        lo_hi = wmin + span * 0.3
        mid_hi = wmin + span * 0.6
        n_high = max(1, round(count * 0.25))
        n_mid = min(max(0, round(count * 0.45)), count - n_high)
        n_low = count - n_high - n_mid
        tiers = [(mid_hi, wmax)] * n_high + [(lo_hi, mid_hi)] * n_mid + [(wmin, lo_hi)] * n_low
        rng.shuffle(tiers)
        weights = [rng.uniform(low, high) for low, high in tiers]
    elif mode == WEIGHT_MODE_CURVE:
        weights = []
        for i in range(count):
            t = i / (count - 1) if count > 1 else 0.0
            if curve == CURVE_ASCENDING:
                value = wmin + t * span
            elif curve == CURVE_DESCENDING:
                value = wmax - t * span
            elif curve == CURVE_VALLEY:
                value = wmin + abs(2 * t - 1) * span
            elif curve == CURVE_PEAK:
                value = wmin + (1 - abs(2 * t - 1)) * span
            else:  # CURVE_FLAT 및 알 수 없는 값
                value = (wmin + wmax) / 2
            weights.append(value)
    else:  # WEIGHT_MODE_RANDOM 및 알 수 없는 값
        weights = [rng.uniform(wmin, wmax) for _ in range(count)]

    # 경계는 폭에 맞추지 않는다 — 사용자가 정한 최소·최대를 넘지 않는 것이 먼저다.
    # (폭 0.3에 최대 0.8이면 0.9로 올림되어 범위를 벗어난다.)
    low, high = normalize_weight(wmin), normalize_weight(wmax)
    return [_clamp(round_weight(w, step), low, high) for w in weights]


def name_signature(slots: tuple[ComboSlot, ...] | list[ComboSlot]) -> str:
    """작가 구성만 보는 서명. 가중치가 달라도 같은 작가들이면 같은 서명이다.

    무작위 조합을 만들 때 이걸로 걸러야 "같은 작가 5명, 가중치만 미세하게 다른"
    조합이 화면을 채우는 일이 없다.
    """
    return "|".join(sorted({slot.name for slot in slots}))


def full_signature(slots: tuple[ComboSlot, ...] | list[ComboSlot]) -> str:
    """가중치까지 보는 서명. 교배 결과가 부모와 완전히 같은지 볼 때 쓴다."""
    return "|".join(f"{slot.name}:{normalize_weight(slot.weight):.2f}" for slot in slots)


def build_slots(
    names: list[str],
    params: ComboGenParams,
    artists: dict[str, ArtistEntry] | None = None,
    rng: random.Random | None = None,
) -> tuple[ComboSlot, ...]:
    """이름 목록에 가중치를 붙여 자리를 만든다.

    작가 명단에서 가중치를 고정해 둔 작가는 그 값을 그대로 쓴다 — 고정한 이유가
    "이 작가는 항상 이 세기로"이므로 무작위 가중치가 덮으면 안 된다.
    """
    weights = generate_weights(
        params.weight_mode,
        len(names),
        params.weight_min,
        params.weight_max,
        params.curve,
        rng,
        params.weight_step,
    )
    artists = artists or {}
    slots: list[ComboSlot] = []
    for name, weight in zip(names, weights, strict=True):
        entry = artists.get(name)
        locked = entry.locked_weight if entry is not None else None
        # 고정 가중치는 사용자가 직접 적은 값이라 증감 폭에도, 최소·최대에도 맞추지 않는다.
        weight = normalize_weight(locked) if locked is not None else weight
        slots.append(ComboSlot(name=name, weight=weight))
    return tuple(slots)


def generate_combos(
    pool: list[ArtistEntry],
    params: ComboGenParams,
    count: int,
    existing: set[str] | None = None,
    rng: random.Random | None = None,
) -> list[Combo]:
    """무작위 조합 `count`개를 만든다 (Gen.1).

    이미 있는 조합(`existing`: `name_signature()` 집합)과 겹치지 않게 뽑는다.
    풀이 작아 더 만들 수 없으면 요청한 개수보다 적게 돌려준다 — 무한 루프 대신
    "만들 수 있는 만큼"이 맞는 동작이다.
    """
    rng = rng or random
    params = params.clamped()
    if count <= 0 or not pool:
        return []

    names = [entry.name for entry in pool]
    by_name = {entry.name: entry for entry in pool}
    high = min(params.max_artists, len(names))
    low = min(params.min_artists, high)

    seen = set(existing or ())
    made: list[Combo] = []
    # 뽑기 실패(중복)가 이어질 수 있으므로 시도 횟수에 상한을 둔다.
    attempts = 0
    max_attempts = max(count * 20, 50)
    while len(made) < count and attempts < max_attempts:
        attempts += 1
        size = rng.randint(low, high)
        picked = rng.sample(names, size)
        signature = name_signature([ComboSlot(name=n) for n in picked])
        if signature in seen:
            continue
        seen.add(signature)
        made.append(Combo(slots=build_slots(picked, params, by_name, rng), generation=1))
    return made


def compose_prompt(base_prompt: str, artist_block: str, position: str) -> str:
    """기본 프롬프트에 작가 블록을 끼워 넣는다.

    작가 태그를 프롬프트 맨 앞에 쓰는 사람이 많은데 원본 프로그램은 항상 뒤에
    붙여서 아쉽다는 지적이 있었다. 그래서 위치를 고를 수 있게 했다.
    """
    base = base_prompt.strip()
    block = artist_block.strip()
    if not block:
        return base
    if not base:
        return block
    if position == INSERT_PLACEHOLDER and ARTIST_PLACEHOLDER in base:
        return base.replace(ARTIST_PLACEHOLDER, block)
    if position == INSERT_SUFFIX:
        return f"{base.rstrip(', ')}, {block}"
    # INSERT_PREFIX, 그리고 자리표시자를 골랐지만 프롬프트에 없는 경우
    return f"{block}, {base.lstrip(', ')}"
