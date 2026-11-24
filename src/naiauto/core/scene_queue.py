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
- 씬 에디터의 편집 도우미 — 이미지 메타데이터로 씬 만들기(`scene_settings_from_image`),
  순서 옮기기(`move_scene`), 랜덤 문법이 든 씬 알아보기(`scene_is_dynamic`)

화면에 씬을 적용하고 잡을 시작하는 일은 뷰(`MainWindow`)가 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .metadata.naiinfo import read_metadata
from .metadata.reuse import ReusableSettings, extract_reusable
from .presets import PresetError, _sanitize_filename
from .prompt_dynamics import has_dynamic_syntax
from .settings.schema import default_data_dir

FORMAT_NAME = "nai-auto-v5-scenes"
FORMAT_VERSION = 1
QUEUE_FILE_NAME = "scene_queue.json"

#: 끌어다 놓으면 메타데이터를 읽어 씬으로 만드는 이미지 (메인 창의 드롭과 같은 형식).
SCENE_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".webp")
#: 끌어다 놓으면 세팅 파일로 읽는 파일 (`파일 → 설정 저장`이 쓰는 형식, V4의 .txt 포함).
SCENE_SETTINGS_SUFFIXES: tuple[str, ...] = (".json", ".txt")

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
    #: True면 모든 씬을 씬 이름 폴더가 아니라 세트 이름 폴더 하나에 저장한다.
    single_folder: bool = False
    #: 마지막으로 저장하거나 불러온 세트 파일의 이름 (확장자 없이). 한 폴더 저장의 폴더 이름이다.
    #: 자동 저장 파일에 함께 남아 앱을 다시 켜도 이어진다. 아직 저장한 적 없으면 "".
    set_name: str = ""
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


def move_scene(scenes: list[Scene], source: int, target: int) -> int:
    """`source`번 씬을 빼서 `target` 자리에 꽂는다 (끌어 놓기). 옮긴 뒤의 자리.

    `target`은 **빼기 전** 목록 기준의 끼울 자리다 (0 ~ len). 자기 자리나 바로 아래에
    놓으면 아무것도 바뀌지 않는다. 범위를 벗어난 `source`면 -1.
    """
    if not 0 <= source < len(scenes):
        return -1
    target = max(0, min(len(scenes), target))
    if target > source:
        target -= 1  # 위에서 하나를 뺐으니 아래쪽 자리가 하나씩 당겨진다
    scenes.insert(target, scenes.pop(source))
    return target


def scene_prompts(settings: dict[str, Any]) -> list[str]:
    """씬 설정에 든 프롬프트 글 전부 — 메인·네거티브·캐릭터 슬롯."""
    texts = [str(settings.get("prompt", "")), str(settings.get("negative_prompt", ""))]
    for character in settings.get("characters") or []:
        if isinstance(character, dict):
            texts.append(str(character.get("prompt", "")))
            texts.append(str(character.get("uc", "")))
    return texts


def scene_is_dynamic(scene: Scene) -> bool:
    """와일드카드·랜덤 문법이 있어 장마다 프롬프트가 달라지는 씬인가."""
    return any(has_dynamic_syntax(text) for text in scene_prompts(scene.settings))


def scene_settings_from_image(reusable: ReusableSettings, base: dict[str, Any]) -> dict[str, Any] | None:
    """이미지에서 복원한 설정을 `base`(보통 지금 화면 값) 위에 얹어 씬 설정을 만든다.

    메인 창의 `메타데이터 적용`(`MainWindow.apply_reusable`)과 같은 규칙이다:

    - 메타데이터의 프롬프트·네거티브에는 품질 태그와 UC 프리셋 글이 **이미 합성되어**
      있다. 그대로 쓰고 품질 태그를 끄고 UC 프리셋을 `none`으로 내린다 (두 번 붙지 않게).
    - 시드는 담지 않는다 — 씬은 여러 장을 뽑는 단위다.
    - 해상도 등급은 비워 크기로 되짚게 한다.

    읽을 정보가 하나도 없으면(NovelAI 이미지가 아니면) None.
    """
    if reusable.is_empty:
        return None
    data = {key: value for key, value in base.items() if key != "seed"}
    if reusable.model is not None:
        data["model"] = reusable.model
    if reusable.prompt is not None:
        data["prompt"] = reusable.prompt
        data["quality_tags"] = False
    if reusable.negative_prompt is not None:
        data["negative_prompt"] = reusable.negative_prompt
        data["uc_preset"] = "none"
        data["quality_tags"] = False
    for field_name in ("steps", "cfg_scale", "cfg_rescale", "sampler", "scheduler"):
        value = getattr(reusable, field_name)
        if value is not None:
            data[field_name] = value
    if reusable.width and reusable.height:
        data["width"], data["height"] = reusable.width, reusable.height
        data["resolution_group"] = ""
    if reusable.characters:
        data["characters"] = [
            {"prompt": c.prompt, "uc": c.uc, "center_x": c.center_x, "center_y": c.center_y}
            for c in reusable.characters
        ]
    return data


def read_scene_from_image(path: Path, base: dict[str, Any]) -> dict[str, Any] | None:
    """이미지 파일의 NovelAI 메타데이터를 읽어 씬 설정으로. 읽지 못하면 None."""
    try:
        metadata = read_metadata(Path(path))
    except Exception:  # 깨진 파일 하나 때문에 여러 장 가져오기가 멈추면 안 된다
        return None
    return scene_settings_from_image(extract_reusable(metadata), base)


def scene_folder(scene_set: SceneSet, scene: Scene) -> str:
    """이 씬의 결과를 담을 하위 폴더 이름 — 한 폴더 저장이면 세트 이름, 아니면 씬 이름.

    한 폴더 저장인데 세트 이름이 없으면(저장 전) 씬 이름으로 물러선다 — 창이 실행 전에
    저장부터 하게 하지만, 여기서 결과를 결과 폴더 맨 위에 흩뿌리지는 않는다.
    """
    if scene_set.single_folder and scene_set.set_name.strip():
        return folder_name(scene_set.set_name.strip())
    return folder_name(scene.name)


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
    "SCENE_IMAGE_SUFFIXES",
    "SCENE_SETTINGS_SUFFIXES",
    "QueueProgress",
    "Scene",
    "SceneQueueController",
    "SceneSet",
    "Step",
    "default_queue_path",
    "folder_name",
    "load_scene_set",
    "load_scene_set_or_empty",
    "move_scene",
    "plan_steps",
    "read_scene_from_image",
    "save_scene_set",
    "scene_folder",
    "scene_is_dynamic",
    "scene_prompts",
    "scene_settings_from_image",
    "total_images",
    "unique_name",
]
