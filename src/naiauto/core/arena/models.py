"""그림체 아레나의 자료형 — 작가 항목·조합·전체 상태 (Qt-free).

아레나는 작가 태그를 무작위로 조합해 실제 이미지를 뽑고, 그 이미지를 1:1로
비교(이상형 월드컵)해 Elo 점수를 매긴 뒤, 상위 조합끼리 교배해 다음 세대를
만드는 흐름이다. 이 모듈은 그 흐름이 다루는 **값**만 정의한다 — 점수 계산은
`elo.py`, 조합 생성은 `combos.py`, 교배는 `evolution.py`, 파일 입출력은
`store.py`에 있다.

저장 형식(`arena.json`)의 스키마 버전은 `ARENA_SCHEMA_VERSION`이다. 사용자가
손으로 고칠 수 있는 파일이므로 `from_dict()`는 값이 이상하면 예외를 던지지 않고
기본값으로 떨어뜨린다 — 한 항목이 깨졌다고 아레나 전체를 잃으면 안 된다.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace

ARENA_SCHEMA_VERSION = 1

#: 새 항목의 시작 점수. Elo의 관례값이자 "아직 아무것도 모른다"는 뜻이다.
DEFAULT_ELO = 1000.0

#: 가중치 증감 폭의 기본값. 사용자가 조합 생성 화면에서 바꿀 수 있다
#: (`ComboGenParams.weight_step`). 0.05는 NovelAI에서 차이가 체감되기 시작하는 단위다.
WEIGHT_STEP = 0.05

#: 증감 폭으로 허용하는 범위. 아래로는 0.01(미세 조정), 위로는 1.0(정수 단위)까지.
#: 0 이하를 허용하면 반올림에서 0으로 나누게 되고, 너무 크면 최소·최대 사이에 값이
#: 하나도 남지 않는다.
MIN_WEIGHT_STEP = 0.01
MAX_WEIGHT_STEP = 1.0

WEIGHT_MODE_RANDOM = "random"
WEIGHT_MODE_BALANCED = "balanced"
WEIGHT_MODE_CURVE = "curve"
WEIGHT_MODES = (WEIGHT_MODE_RANDOM, WEIGHT_MODE_BALANCED, WEIGHT_MODE_CURVE)

CURVE_FLAT = "flat"
CURVE_ASCENDING = "ascending"
CURVE_DESCENDING = "descending"
CURVE_VALLEY = "valley"  # V자: 양끝 높고 중앙 낮음
CURVE_PEAK = "peak"  # 역V자: 중앙 높고 양끝 낮음
CURVE_PRESETS = (CURVE_DESCENDING, CURVE_ASCENDING, CURVE_PEAK, CURVE_VALLEY, CURVE_FLAT)

#: 조합한 작가 블록을 프롬프트 어디에 넣을지.
#: "prefix" — 맨 앞 (작가 태그를 앞에 쓰는 사람이 많다)
#: "suffix" — 맨 뒤
#: "placeholder" — 프롬프트 안의 `<artist>` 자리에 (없으면 맨 앞으로 떨어진다)
INSERT_PREFIX = "prefix"
INSERT_SUFFIX = "suffix"
INSERT_PLACEHOLDER = "placeholder"
INSERT_POSITIONS = (INSERT_PREFIX, INSERT_SUFFIX, INSERT_PLACEHOLDER)

#: `<artist>` 자리표시자 — INSERT_PLACEHOLDER일 때 이 문자열이 조합으로 바뀐다.
ARTIST_PLACEHOLDER = "<artist>"


def _as_float(value: object, default: float) -> float:
    """JSON에서 읽은 값을 float으로. 실패하면 기본값 (bool은 숫자로 보지 않는다)."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # NaN/inf는 점수 계산과 정렬을 통째로 망가뜨린다
    return result if result == result and abs(result) != float("inf") else default


def _as_int(value: object, default: int, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def new_id() -> str:
    """조합 식별자. 이미지 파일명에도 쓰이므로 파일명에 안전한 문자만 쓴다."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class ComboSlot:
    """조합의 한 자리 — 작가 이름과 그 자리의 가중치.

    `name`은 `artist:` 접두사를 **떼어 낸** 이름이다. 접두사는 프롬프트로 낼 때
    `combos.format_artist_block()`이 붙인다 (붙일지 말지는 설정).
    """

    name: str
    weight: float = 1.0

    def to_dict(self) -> dict:
        return {"name": self.name, "weight": self.weight}

    @classmethod
    def from_dict(cls, data: object) -> ComboSlot | None:
        """이름이 비어 있으면 None — 자리로 세지 않는다."""
        if not isinstance(data, dict):
            return None
        name = _as_str(data.get("name")).strip()
        if not name:
            return None
        return cls(name=name, weight=_as_float(data.get("weight"), 1.0))


@dataclass
class ArtistEntry:
    """작가 명단의 한 줄. 조합에 뽑히는 후보이자, 그 자체로 순위가 매겨진다.

    조합이 대결할 때마다 그 조합에 들어 있던 작가들도 점수를 나눠 받는다
    (`elo.propagate_to_artists`). 여러 조합에 걸쳐 꾸준히 이긴 작가가 위로 올라온다.
    """

    name: str
    #: 가중치 고정. None이면 조합할 때 무작위로 정해지고, 값이 있으면 항상 그 값을 쓴다
    #: (원본 프로그램의 "더블 클릭으로 가중치 영구 고정").
    locked_weight: float | None = None
    elo: float = DEFAULT_ELO
    matches: int = 0  # 이 작가가 낀 조합이 치른 대결 수
    wins: int = 0
    uses: int = 0  # 조합에 뽑힌 횟수 (검출수)
    #: 사용자가 작가별로 남기는 자유 메모 (예: "선화 위주", "이 작가는 배경이 약함").
    #: 점수·조합에는 영향을 주지 않고, 명단에서 보고 편집만 한다.
    note: str = ""

    @property
    def win_rate(self) -> float:
        """0.0~1.0. 대결 기록이 없으면 0.0."""
        return self.wins / self.matches if self.matches > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "locked_weight": self.locked_weight,
            "elo": self.elo,
            "matches": self.matches,
            "wins": self.wins,
            "uses": self.uses,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: object) -> ArtistEntry | None:
        if not isinstance(data, dict):
            return None
        name = _as_str(data.get("name")).strip()
        if not name:
            return None
        raw_lock = data.get("locked_weight")
        return cls(
            name=name,
            locked_weight=None if raw_lock is None else _as_float(raw_lock, 1.0),
            elo=_as_float(data.get("elo"), DEFAULT_ELO),
            matches=_as_int(data.get("matches"), 0, minimum=0),
            wins=_as_int(data.get("wins"), 0, minimum=0),
            uses=_as_int(data.get("uses"), 0, minimum=0),
            note=_as_str(data.get("note")),
        )


@dataclass
class Combo:
    """작가 조합 하나 — 아레나에서 겨루는 단위.

    `image_path`가 비어 있으면 아직 그림이 없다는 뜻이다 (생성 대기 중이거나 실패).
    그림이 없는 조합은 월드컵에 나가지 못한다.
    """

    slots: tuple[ComboSlot, ...]
    id: str = field(default_factory=new_id)
    elo: float = DEFAULT_ELO
    matches: int = 0
    wins: int = 0
    favorite: bool = False
    locked: bool = False  # 잠금: 일괄 정리에서 살아남고, 교배 부모 후보에 항상 포함
    memo: str = ""
    generation: int = 1
    parents: tuple[str, ...] = ()  # 부모 조합 id (Gen.1은 비어 있다)
    image_path: str = ""  # images/ 안의 파일명 (폴더를 옮겨도 살아남게 상대경로)
    seed: int = 0
    created_at: float = field(default_factory=time.time)
    #: LLM 그림체 판독 결과. `judge_score`는 참조와의 유사도 0~100, 아직 판독하지
    #: 않았으면 -1(=모름). `judge_reason`은 모델이 준 한 줄 근거다. 사람 Elo와는
    #: 완전히 별개로, "LLM 추천 랭킹"에만 쓴다.
    judge_score: int = -1
    judge_reason: str = ""
    #: **이 점수를 매긴 모델의 이름.** 모델을 바꿔 가며 판독해 보고 어느 쪽이 사람
    #: 취향과 잘 맞는지 견주려면, 점수 옆에 누가 매겼는지가 함께 남아야 한다.
    #: 설정의 모델명이 아니라 서버가 **실제로 고른** 식별자다 (빈 값이면 자동 선택).
    judge_model: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image_path)

    @property
    def has_judge_score(self) -> bool:
        """LLM 판독 점수가 있는지 (0~100). -1이면 아직 판독 전."""
        return self.judge_score >= 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches if self.matches > 0 else 0.0

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(slot.name for slot in self.slots)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slots": [slot.to_dict() for slot in self.slots],
            "elo": self.elo,
            "matches": self.matches,
            "wins": self.wins,
            "favorite": self.favorite,
            "locked": self.locked,
            "memo": self.memo,
            "generation": self.generation,
            "parents": list(self.parents),
            "image_path": self.image_path,
            "seed": self.seed,
            "created_at": self.created_at,
            "judge_score": self.judge_score,
            "judge_reason": self.judge_reason,
            "judge_model": self.judge_model,
        }

    @classmethod
    def from_dict(cls, data: object) -> Combo | None:
        """자리가 하나도 없으면 None — 빈 조합은 프롬프트로 낼 수 없다."""
        if not isinstance(data, dict):
            return None
        raw_slots = data.get("slots")
        slots: list[ComboSlot] = []
        if isinstance(raw_slots, list):
            for raw in raw_slots:
                slot = ComboSlot.from_dict(raw)
                if slot is not None:
                    slots.append(slot)
        if not slots:
            return None
        raw_parents = data.get("parents")
        parents = tuple(p for p in raw_parents if isinstance(p, str)) if isinstance(raw_parents, list) else ()
        return cls(
            slots=tuple(slots),
            id=_as_str(data.get("id")) or new_id(),
            elo=_as_float(data.get("elo"), DEFAULT_ELO),
            matches=_as_int(data.get("matches"), 0, minimum=0),
            wins=_as_int(data.get("wins"), 0, minimum=0),
            favorite=bool(data.get("favorite", False)),
            locked=bool(data.get("locked", False)),
            memo=_as_str(data.get("memo")),
            generation=_as_int(data.get("generation"), 1, minimum=1),
            parents=parents,
            image_path=_as_str(data.get("image_path")),
            seed=_as_int(data.get("seed"), 0),
            created_at=_as_float(data.get("created_at"), 0.0),
            judge_score=_as_int(data.get("judge_score"), -1),
            judge_reason=_as_str(data.get("judge_reason")),
            judge_model=_as_str(data.get("judge_model")),
        )


@dataclass
class JudgeRun:
    """마지막 LLM 판독이 **어떤 조건으로** 돌았는지 — 모델 간 비교의 기준선.

    조합마다 남는 `Combo.judge_model`이 "누가 이 점수를 매겼나"에 답한다면, 여기에는
    그 판독 한 회차의 조건이 남는다: 참조를 몇 장 썼는지, 채점 지시를 손봤는지,
    몇 개를 성공/실패했는지. 같은 후보를 모델 A와 B로 판독해 견줄 때, 점수만으로는
    "참조를 3장 쓴 판독과 5장 쓴 판독"을 구분할 수 없기 때문이다.

    `arena.json`에 함께 저장되므로 앱을 껐다 켜도 남는다. 판독 점수를 비우면 이것도
    함께 지운다 — 지워진 점수의 조건만 남아 있으면 거짓말이 된다.
    """

    #: 서버가 실제로 고른 모델 식별자 (설정값이 아니라 결과).
    model: str = ""
    host: str = ""
    #: 요청 제한 시간(초). 0 이하면 무제한.
    timeout: float = 0.0
    #: 이번 판독에 실제로 넣은 참조 장수와, 설정에 걸어 둔 상한.
    references: int = 0
    max_references: int = 0
    #: 채점 지시(시스템 프롬프트)를 손댔는지. 내용 자체는 길어 담지 않는다.
    custom_prompt: bool = False
    scored: int = 0
    failed: int = 0
    #: 끝난 시각 (ISO 8601, 로컬 시간).
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "host": self.host,
            "timeout": self.timeout,
            "references": self.references,
            "max_references": self.max_references,
            "custom_prompt": self.custom_prompt,
            "scored": self.scored,
            "failed": self.failed,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> JudgeRun | None:
        if not isinstance(data, dict):
            return None
        return cls(
            model=_as_str(data.get("model")),
            host=_as_str(data.get("host")),
            timeout=_as_float(data.get("timeout"), 0.0),
            references=_as_int(data.get("references"), 0, minimum=0),
            max_references=_as_int(data.get("max_references"), 0, minimum=0),
            custom_prompt=bool(data.get("custom_prompt", False)),
            scored=_as_int(data.get("scored"), 0, minimum=0),
            failed=_as_int(data.get("failed"), 0, minimum=0),
            finished_at=_as_str(data.get("finished_at")),
        )


@dataclass(frozen=True)
class ComboGenParams:
    """조합을 만들 때 쓰는 값들. UI 설정(P2의 `AppSettings.arena`)이 이 모양으로 넘어온다.

    frozen인 이유는 생성 스레드가 이 값을 읽는 동안 UI가 바꾸면 안 되기 때문이다 —
    프로젝트의 불변 요청 흐름 원칙과 같다.
    """

    min_artists: int = 3
    max_artists: int = 6
    weight_mode: str = WEIGHT_MODE_BALANCED
    weight_min: float = 0.8
    weight_max: float = 1.8
    curve: str = CURVE_DESCENDING
    use_prefix: bool = True  # 프롬프트에 `artist:` 접두사를 붙일지
    #: 가중치를 이 폭의 배수로 맞춘다 (0.01~1.0). 0.05면 1.15·1.20·1.25처럼,
    #: 0.01이면 1.16·1.17처럼 잘게 나온다.
    weight_step: float = WEIGHT_STEP
    mutation_rate: float = 0.15  # 교배 시 자리 하나가 다른 작가로 바뀔 확률
    weight_jitter: float = 0.15  # 교배 시 물려받은 가중치를 흔드는 폭 (±)

    def clamped(self) -> ComboGenParams:
        """UI/설정 파일에서 온 값을 쓸 수 있는 범위로 정리한다.

        min > max처럼 뒤집힌 입력은 거부하지 않고 맞바꾼다 — 사용자가 스핀박스를
        만지는 도중에도 조합 생성이 예외로 죽지 않아야 한다.
        """
        low, high = sorted((max(1, self.min_artists), max(1, self.max_artists)))
        wmin, wmax = sorted((self.weight_min, self.weight_max))
        return replace(
            self,
            min_artists=low,
            max_artists=high,
            weight_mode=self.weight_mode if self.weight_mode in WEIGHT_MODES else WEIGHT_MODE_BALANCED,
            weight_min=wmin,
            weight_max=wmax,
            weight_step=min(MAX_WEIGHT_STEP, max(MIN_WEIGHT_STEP, self.weight_step)),
            curve=self.curve if self.curve in CURVE_PRESETS else CURVE_DESCENDING,
            mutation_rate=min(1.0, max(0.0, self.mutation_rate)),
            weight_jitter=max(0.0, self.weight_jitter),
        )


@dataclass
class ArenaState:
    """아레나 전체 — 작가 명단과 조합 목록. `store.py`가 이걸 통째로 읽고 쓴다."""

    artists: list[ArtistEntry] = field(default_factory=list)
    combos: list[Combo] = field(default_factory=list)
    total_matches: int = 0  # 지금까지 진행한 대결 수 (통계 표시용)
    #: 마지막 LLM 판독의 조건. 한 번도 안 돌렸으면 None.
    judge_run: JudgeRun | None = None

    # ------------------------------------------------------------------ 조회

    def artist(self, name: str) -> ArtistEntry | None:
        for entry in self.artists:
            if entry.name == name:
                return entry
        return None

    def combo(self, combo_id: str) -> Combo | None:
        for combo in self.combos:
            if combo.id == combo_id:
                return combo
        return None

    def artist_names(self) -> list[str]:
        return [entry.name for entry in self.artists]

    # ------------------------------------------------------------------ 변경

    def add_artists(self, names: list[str]) -> int:
        """이미 있는 이름은 건너뛰고 새 작가만 추가한다. 추가한 수를 돌려준다."""
        existing = {entry.name for entry in self.artists}
        added = 0
        for name in names:
            cleaned = name.strip()
            if not cleaned or cleaned in existing:
                continue
            self.artists.append(ArtistEntry(name=cleaned))
            existing.add(cleaned)
            added += 1
        return added

    def remove_artists(self, names: set[str]) -> int:
        """작가를 명단에서 지운다. 이미 만들어진 조합은 그대로 둔다 —
        그 조합의 그림과 점수는 여전히 유효하기 때문이다."""
        before = len(self.artists)
        self.artists = [entry for entry in self.artists if entry.name not in names]
        return before - len(self.artists)

    def remove_combos(self, ids: set[str]) -> list[Combo]:
        """조합을 지우고 지워진 것들을 돌려준다 (호출한 쪽이 이미지 파일을 지운다)."""
        removed = [combo for combo in self.combos if combo.id in ids]
        if removed:
            self.combos = [combo for combo in self.combos if combo.id not in ids]
        return removed

    def reset_matches(self) -> None:
        """대전 기록만 지운다 — 작가 명단과 조합, 그림은 그대로 남는다.

        작가 명단까지 날아가는 게 불편하다는 지적이 있어 둘을 나눴다.
        """
        self.total_matches = 0
        for combo in self.combos:
            combo.elo = DEFAULT_ELO
            combo.matches = 0
            combo.wins = 0
        for entry in self.artists:
            entry.elo = DEFAULT_ELO
            entry.matches = 0
            entry.wins = 0

    # ------------------------------------------------------------ 직렬화

    def to_dict(self) -> dict:
        return {
            "version": ARENA_SCHEMA_VERSION,
            "total_matches": self.total_matches,
            "artists": [entry.to_dict() for entry in self.artists],
            "combos": [combo.to_dict() for combo in self.combos],
            "judge_run": None if self.judge_run is None else self.judge_run.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> ArenaState:
        """깨진 항목은 조용히 버리고 나머지를 살린다. 절대 예외를 던지지 않는다."""
        if not isinstance(data, dict):
            return cls()
        artists: list[ArtistEntry] = []
        seen: set[str] = set()
        raw_artists = data.get("artists")
        if isinstance(raw_artists, list):
            for raw in raw_artists:
                entry = ArtistEntry.from_dict(raw)
                if entry is not None and entry.name not in seen:
                    artists.append(entry)
                    seen.add(entry.name)
        combos: list[Combo] = []
        seen_ids: set[str] = set()
        raw_combos = data.get("combos")
        if isinstance(raw_combos, list):
            for raw in raw_combos:
                combo = Combo.from_dict(raw)
                if combo is None:
                    continue
                if combo.id in seen_ids:  # 손으로 복사해 붙인 중복 id는 새로 뽑는다
                    combo.id = new_id()
                combos.append(combo)
                seen_ids.add(combo.id)
        return cls(
            artists=artists,
            combos=combos,
            total_matches=_as_int(data.get("total_matches"), 0, minimum=0),
            judge_run=JudgeRun.from_dict(data.get("judge_run")),
        )
