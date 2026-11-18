"""씬 큐 — 씬마다 설정과 매수를 정해 두고 한 번에 돌린다 (Qt-free).

`세팅별 연속 생성`은 세팅 파일을 매번 다시 고르고, 파일마다 같은 매수를 한 장씩 번갈아
뽑는다. 씬 큐는 그 목록을 **기억하고**, 씬마다 매수를 따로 주며, 차례로(씬 1을 다 뽑고
씬 2) 또는 번갈아(한 장씩 돌아가며) 돌린다.

씬 하나는 `파일 → 설정 저장`이 쓰는 것과 같은 모양의 설정(`settings_file.to_dict`)에
이름·매수·켬/끔을 붙인 것이다. 그래서 세팅 파일을 그대로 씬으로 가져올 수 있고, 씬을
적용하는 길도 세팅 파일을 불러올 때와 같다 (`<lock>` 구간이 남는 것까지).

이 모듈이 맡는 것:

- 자료형과 파일 입출력 (`Scene`, `SceneSet`, `load_scene_set`, `save_scene_set`)
- 실행 계획 (`plan_steps`) — 어떤 씬을 몇 장씩 어떤 순서로
- 진행 판정 (`SceneQueueController`) — 잡 하나가 끝났을 때 다음으로 갈지

화면에 씬을 적용하고 잡을 시작하는 일은 뷰(`MainWindow`)가 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .presets import PresetError, _sanitize_filename
from .settings.schema import default_data_dir

FORMAT_NAME = "nai-auto-v5-scenes"
FORMAT_VERSION = 1
QUEUE_FILE_NAME = "scene_queue.json"

#: 씬 하나의 매수 범위. 0(무한)은 받지 않는다 — 끝나지 않는 씬 뒤의 씬은 영영 오지 않는다.
SCENE_COUNT_RANGE: tuple[int, int] = (1, 9999)


def default_queue_path() -> Path:
    """지금 큐를 자동으로 남겨 두는 파일. 테스트는 이 함수를 임시 폴더로 바꿔 끼운다."""
    return default_data_dir() / QUEUE_FILE_NAME


class Scene(BaseModel):
    """씬 하나 — 이름, 매수, 켬/끔, 생성 설정."""

    name: str
    count: int = 1
    enabled: bool = True
    #: `settings_file.to_dict()` 형식. 시드는 담지 않는다 (씬은 여러 장을 뽑는 단위다).
    settings: dict[str, Any] = Field(default_factory=dict)

    def clamped_count(self) -> int:
        low, high = SCENE_COUNT_RANGE
        return max(low, min(high, self.count))


class SceneSet(BaseModel):
    """씬 목록 + 실행 방식. 파일 하나에 통째로 담긴다."""

    format: str = FORMAT_NAME
    version: int = FORMAT_VERSION
    #: True면 씬을 한 장씩 번갈아 돈다 (세팅별 연속 생성과 같은 순서).
    interleave: bool = False
    scenes: list[Scene] = Field(default_factory=list)


def load_scene_set(path: Path) -> SceneSet:
    """씬 세트 파일을 읽는다. 읽을 수 없거나 형식이 다르면 `PresetError`."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PresetError(
            f"씬 파일을 읽을 수 없습니다: {path}",
            i18n_key="errors.settings_file_unreadable",
            i18n_args=(str(path),),
        ) from e
    if not isinstance(raw, dict) or raw.get("format") != FORMAT_NAME:
        raise PresetError(
            f"씬 파일 형식이 올바르지 않습니다: {path}",
            i18n_key="errors.settings_file_format",
            i18n_args=(str(path),),
        )
    try:
        return SceneSet.model_validate(raw)
    except ValidationError as e:
        raise PresetError(
            f"씬 파일 값이 올바르지 않습니다: {e}",
            i18n_key="errors.settings_file_invalid",
            i18n_args=(str(e),),
        ) from e


def load_scene_set_or_empty(path: Path) -> SceneSet:
    """자동 저장 파일용 — 없거나 깨졌으면 빈 세트로 시작한다."""
    try:
        return load_scene_set(path)
    except PresetError:
        return SceneSet()


def save_scene_set(path: Path, scene_set: SceneSet) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(scene_set.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def unique_name(name: str, existing: list[str]) -> str:
    """겹치지 않는 씬 이름 — `풍경`, `풍경 2`, `풍경 3` …

    이름이 곧 저장 폴더 이름이라, 겹치면 두 씬의 그림이 한 폴더에 섞인다.
    """
    base = name.strip() or "scene"
    taken = set(existing)
    if base not in taken:
        return base
    index = 2
    while f"{base} {index}" in taken:
        index += 1
    return f"{base} {index}"


def folder_name(scene_name: str) -> str:
    """씬 이름을 결과 폴더의 하위 폴더 이름으로 (파일 이름에 못 쓰는 글자를 바꾼다)."""
    return _sanitize_filename(scene_name)


@dataclass(frozen=True)
class Step:
    """잡 하나 — `scene_index`번 씬을 `count`장."""

    scene_index: int
    count: int


def plan_steps(scenes: list[Scene], *, interleave: bool = False) -> list[Step]:
    """켜진 씬으로 실행 순서를 만든다.

    - 차례로(기본): 씬마다 잡 하나 — `[씬1×3, 씬2×5]`
    - 번갈아: 한 장짜리 잡을 돌아가며 — `[씬1, 씬2, 씬1, 씬2, 씬1, 씬2, 씬2, 씬2]`.
      매수를 다 채운 씬은 빠지고 남은 씬끼리 계속 돈다.
    """
    active = [(index, scene.clamped_count()) for index, scene in enumerate(scenes) if scene.enabled]
    if not interleave:
        return [Step(index, count) for index, count in active]
    steps: list[Step] = []
    remaining = dict(active)
    while remaining:
        for index in [index for index, _count in active if index in remaining]:
            steps.append(Step(index, 1))
            remaining[index] -= 1
            if remaining[index] <= 0:
                del remaining[index]
    return steps


def total_images(steps: list[Step]) -> int:
    return sum(step.count for step in steps)


@dataclass
class QueueProgress:
    """`job_finished()`의 판정 (`settings_batch.BatchProgress`와 같은 모양)."""

    continuing: bool
    completed: int
    stopped: bool


class SceneQueueController:
    """큐 실행의 상태와 진행 판정.

    진행 프로토콜:
        start(steps) → [next_step() → 잡 시작 → job_finished(...) → 계속?] 반복 → 끝
    """

    def __init__(self) -> None:
        self._steps: list[Step] = []
        self._position = -1
        self.completed = 0
        self.stop_requested = False

    @property
    def active(self) -> bool:
        return bool(self._steps)

    @property
    def total(self) -> int:
        return total_images(self._steps)

    @property
    def position(self) -> int:
        """지금 잡의 자리 (0부터). 시작 전에는 -1."""
        return self._position

    @property
    def step_count(self) -> int:
        return len(self._steps)

    def start(self, steps: list[Step]) -> None:
        self._steps = list(steps)
        self._position = -1
        self.completed = 0
        self.stop_requested = False

    def reset(self) -> None:
        self._steps = []

    def request_stop(self) -> None:
        self.stop_requested = True

    def next_step(self) -> Step | None:
        """다음 잡으로 넘어가 그 잡을 돌려준다. 남은 것이 없으면 None."""
        if self._position + 1 >= len(self._steps):
            return None
        self._position += 1
        return self._steps[self._position]

    def job_finished(self, *, completed: int, error: bool, stopped: bool) -> QueueProgress:
        """잡 하나가 끝났다 — `completed`는 그 잡이 실제로 만든 장수.

        오류나 중지로 끝났어도 그때까지 만든 장수는 누적에 넣는다 (상태바가 "N장 생성"을
        보여 준다). 계속하지 않으면 큐를 끝낸다(`reset`).
        """
        self.completed += max(0, completed)
        stopped = stopped or self.stop_requested
        continuing = not error and not stopped and self._position + 1 < len(self._steps)
        if not continuing:
            self.reset()
        return QueueProgress(continuing=continuing, completed=self.completed, stopped=stopped)


__all__ = [
    "FORMAT_NAME",
    "QUEUE_FILE_NAME",
    "SCENE_COUNT_RANGE",
    "QueueProgress",
    "Scene",
    "SceneQueueController",
    "SceneSet",
    "Step",
    "default_queue_path",
    "folder_name",
    "load_scene_set",
    "load_scene_set_or_empty",
    "plan_steps",
    "save_scene_set",
    "total_images",
    "unique_name",
]
