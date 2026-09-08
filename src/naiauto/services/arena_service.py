"""아레나 이미지 프리페치 — 조합 목록을 받아 그림을 미리 뽑아 둔다 (Qt-free).

**직렬성**: NovelAI는 계정 단위로 요청을 하나씩 보내는 것이 규칙이다. 그래서 여기서
워커를 새로 만들지 않고 기존 `GenerationService`(worker=1)에 잡을 위임한다. 메인 배치
생성이 도는 중에는 아레나가 시작되지 않고(`ArenaBusyError`), 반대도 마찬가지다.

**공정한 비교**: 한 세션의 모든 조합은 같은 시드·같은 기본 프롬프트·같은 해상도로
뽑는다. 조합마다 시드가 달라지면 구도와 포즈가 함께 바뀌어 그림체 차이를 덮어 버린다
(원본 프로그램의 가장 큰 약점이었다). 그래서 잡을 `randomize_seed=False`로 낸다.

**이벤트**: `GenerationService`는 콜백 하나로만 바깥과 통신하고, 그 콜백은 이미 UI의
Qt 브리지가 잡고 있다. 그래서 아레나는 콜백을 가로채지 않고, UI가 받은 이벤트를
`handle_event()`로 넘겨 준다 — 아레나 잡이 도는 동안에는 True(내가 처리했다)를
돌려주므로 메인 창의 진행 표시가 아레나 이미지에 반응하지 않는다.
"""

from __future__ import annotations

import dataclasses
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.api.models import GenerationRequest
from ..core.arena.archive import ArchiveError, ArchiveSummary, export_archive, import_archive
from ..core.arena.combos import compose_prompt, format_artist_block
from ..core.arena.finale import combo_for_index
from ..core.arena.models import INSERT_PREFIX, ArenaState, Combo
from ..core.arena.store import ArenaStore
from ..core.prompt_dynamics import has_dynamic_syntax
from .events import (
    GenerationEvent,
    ImageCompleted,
    ImageRetrying,
    ImageStarted,
    JobFinished,
    WaitingNext,
)
from .generation_service import GenerationJob, GenerationService

logger = logging.getLogger(__name__)

#: 아레나 이미지 파일명. 조합 id는 파일명에 안전한 16진수라 그대로 쓴다.
#: 시드는 세션 전체가 같은 값이므로 이름만으로는 구분되지 않지만, 저장 쪽이 충돌 시
#: `_1`, `_2`를 붙여 주고 실제 경로는 완료 이벤트에서 받아 기록한다.
ARENA_FILENAME_TEMPLATE = "{datetime}_{seed}"


class ArenaBusyError(RuntimeError):
    """다른 생성이 도는 중이라 아레나를 시작할 수 없다."""


@dataclass(frozen=True)
class ArenaSpec:
    """한 세션의 고정 조건. 조합 말고는 전부 같아야 비교가 공정하다.

    `request.prompt`는 **작가 블록이 빠진** 기본 프롬프트다 — 조합마다 여기에
    작가 블록을 끼워 넣는다.
    """

    request: GenerationRequest
    seed: int = 0  # 0 이하 = 시작할 때 하나 뽑는다
    insert_position: str = INSERT_PREFIX
    use_prefix: bool = True
    image_format: str = "png"
    delay_seconds: float = 3.0


@dataclass(frozen=True)
class ArenaEvent:
    """아레나 이벤트의 베이스."""


@dataclass(frozen=True)
class ArenaBatchStarted(ArenaEvent):
    total: int
    seed: int


@dataclass(frozen=True)
class ArenaImageStarted(ArenaEvent):
    index: int  # 1부터
    total: int
    combo_id: str


@dataclass(frozen=True)
class ArenaImageReady(ArenaEvent):
    index: int
    total: int
    combo_id: str
    path: str


@dataclass(frozen=True)
class ArenaImageRetrying(ArenaEvent):
    """일시적 오류로 같은 장을 다시 시도하기 전 대기 중."""

    index: int
    total: int
    reason: str
    wait_seconds: float
    attempt: int


@dataclass(frozen=True)
class ArenaWaitingNext(ArenaEvent):
    """다음 장까지 딜레이 대기 중."""

    next_index: int
    total: int
    wait_seconds: float


@dataclass(frozen=True)
class ArenaBatchFinished(ArenaEvent):
    completed: int
    stopped: bool = False
    error: str | None = None
    error_type: str | None = None


def build_request(spec: ArenaSpec, combo: Combo, seed: int) -> GenerationRequest:
    """조합 하나를 그릴 요청. 기본 프롬프트에 작가 블록을 끼우고 시드를 고정한다."""
    block = format_artist_block(combo.slots, spec.use_prefix)
    prompt = compose_prompt(spec.request.prompt, block, spec.insert_position)
    return dataclasses.replace(spec.request, prompt=prompt, seed=seed)


def build_finale_provider(
    base: GenerationRequest,
    combos: list[Combo],
    per_combo: int,
    *,
    insert_position: str = INSERT_PREFIX,
    use_prefix: bool = True,
) -> Callable[[int], GenerationRequest]:
    """통계 결산용 — 등수 순서대로 장마다 요청을 만들어 주는 함수.

    `GenerationJob.request_provider`에 그대로 꽂는다. 메인 창의 기존 생성
    파이프라인이 이걸 받아 돌리므로, 결과가 **결과 폴더**에 쌓이고 갤러리에도
    뜬다 — 아레나 폴더의 습작과 섞이지 않는다.

    시드는 건드리지 않는다. 메인 창의 `시드 랜덤` 설정을 그대로 따르는 편이
    낫다 — 켜져 있으면 장마다 다른 그림이, 꺼져 있으면 **같은 시드로 그림체만**
    바뀐 한 벌이 나온다 (월드컵과 같은 비교 방식).

    워커 스레드에서 불리므로 위젯을 만지지 않는다 — 조합 목록을 미리 복사해 둔다.
    """
    ordered = list(combos)
    step = max(1, per_combo)

    def provider(index: int) -> GenerationRequest:
        combo = combo_for_index(ordered, step, index)
        if combo is None:
            return base
        block = format_artist_block(combo.slots, use_prefix)
        return dataclasses.replace(base, prompt=compose_prompt(base.prompt, block, insert_position))

    return provider


#: `dynamic_prompt_parts()`가 돌려주는 자리 이름 — UI가 i18n 키로 옮긴다.
PART_PROMPT = "prompt"
PART_NEGATIVE = "negative"
PART_CHARACTER = "character"


def dynamic_prompt_parts(spec: ArenaSpec) -> tuple[str, ...]:
    """매 장 달라지는 문법(와일드카드·`<a|b>` 등)이 들어 있는 **자리 이름**들.

    어디에 있는지까지 알려 줘야 고칠 수 있다. 프롬프트는 멀쩡한데 캐릭터 프롬프트에
    와일드카드가 있는 경우, "프롬프트에 문제가 있다"고만 하면 찾을 수가 없다.
    """
    request = spec.request
    parts: list[str] = []
    if has_dynamic_syntax(request.prompt):
        parts.append(PART_PROMPT)
    if has_dynamic_syntax(request.negative_prompt):
        parts.append(PART_NEGATIVE)
    if any(has_dynamic_syntax(c.prompt) or has_dynamic_syntax(c.uc) for c in request.characters):
        parts.append(PART_CHARACTER)
    return tuple(parts)


def base_prompt_is_dynamic(spec: ArenaSpec) -> bool:
    """기본 프롬프트에 매 장 달라지는 문법이 하나라도 있는지.

    있으면 조합마다 장면이 달라져 그림체만 비교한다는 전제가 깨진다. 막지는 않고
    UI가 경고한다 — 일부러 그러는 사람도 있다.
    """
    return bool(dynamic_prompt_parts(spec))


def pending_combos(state: ArenaState) -> list[Combo]:
    """아직 그림이 없는 조합 — 다음에 뽑아야 할 것들."""
    return [combo for combo in state.combos if not combo.has_image]


def ready_combos(state: ArenaState) -> list[Combo]:
    """월드컵에 올릴 수 있는 조합 (그림이 있는 것)."""
    return [combo for combo in state.combos if combo.has_image]


class ArenaService:
    """아레나의 상태와 이미지 생성 큐를 함께 들고 있는 계층.

    `state`는 UI가 직접 읽고 고친다 (테이블 편집·즐겨찾기 등). 이 클래스는 그 상태를
    **저장**하고, 그림이 없는 조합의 그림을 **채우는** 일만 맡는다.
    """

    def __init__(
        self,
        generation: GenerationService,
        store: ArenaStore,
        state: ArenaState | None = None,
        on_event: Callable[[ArenaEvent], None] = lambda event: None,
        rng: random.Random | None = None,
    ) -> None:
        self._generation = generation
        self._store = store
        #: 구독자 목록 — 화면이 여럿(탭)이라 하나로는 모자란다. 생성자 인자는 첫 구독자다.
        self._subscribers: list[Callable[[ArenaEvent], None]] = [on_event]
        self._rng = rng or random.Random()
        self.state = state if state is not None else store.load()
        # 실행 중인 묶음 — start()에서 채우고 끝나면 비운다. 워커 스레드가
        # `_request_for_index`로 읽기만 하므로 잡이 도는 동안에는 바꾸지 않는다.
        self._batch: list[Combo] = []
        self._spec: ArenaSpec | None = None
        self._seed = 0
        self._completed = 0
        self._running = False
        #: 마지막 삭제 — `undo_last_removal()`이 되돌린다. 한 단계만 기억한다
        #: (실수를 되돌리기 위한 안전망이지, 편집 이력이 아니다).
        self._last_removed: list[Combo] = []
        self._last_removed_images: bool = False

    # ------------------------------------------------------------------ 상태

    @property
    def store(self) -> ArenaStore:
        return self._store

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def batch_total(self) -> int:
        return len(self._batch)

    @property
    def session_seed(self) -> int:
        """이번 세션이 쓰는 시드 (아직 한 번도 안 뽑았으면 0).

        모든 조합이 이 시드를 공유하므로, 설정에 적어 두면 다음에 켜도 같은 조건으로
        이어 뽑을 수 있다.
        """
        return self._seed

    def save(self) -> bool:
        return self._store.save(self.state)

    # ------------------------------------------------------------ 삭제·복구

    @property
    def can_undo(self) -> bool:
        """되돌릴 삭제가 남아 있는지."""
        return bool(self._last_removed)

    @property
    def last_removed_count(self) -> int:
        return len(self._last_removed)

    def remove_combos(self, combos: list[Combo], delete_images: bool = False) -> int:
        """조합을 지운다. 화면들이 쓰는 **유일한** 삭제 경로다.

        `delete_images=False`(기본)면 그림 파일은 그대로 두고 조합 기록만 지운다.
        그래야 실수로 지워도 `undo_last_removal()`이 그림까지 온전히 되돌릴 수 있다.
        남은 파일은 어떤 조합도 가리키지 않으므로, 통계 탭의 `안 쓰는 그림 정리`로
        언제든 치울 수 있다.

        `delete_images=True`면 파일까지 지운다 — 되돌려도 그림은 돌아오지 않고,
        그 조합은 `대기` 상태로 복구된다.
        """
        targets = [combo for combo in combos if combo is not None]
        if not targets:
            return 0
        if delete_images:
            self._store.delete_images(targets)
        self.state.remove_combos({combo.id for combo in targets})
        self._last_removed = targets
        self._last_removed_images = delete_images
        self.save()
        return len(targets)

    def undo_last_removal(self) -> int:
        """마지막 삭제를 되돌린다. 되살린 수를 돌려준다 (되돌릴 게 없으면 0).

        그림 파일까지 지운 삭제였다면 그림은 돌아오지 않는다 — 그 조합은 기록만
        살아나 `대기` 상태가 되므로, 다시 뽑으면 된다. 점수와 전적은 그대로다.
        """
        if not self._last_removed:
            return 0
        restored = self._last_removed
        if self._last_removed_images:
            for combo in restored:
                combo.image_path = ""
        else:
            # 파일이 정말 남아 있는지 확인한다 — 그 사이에 `안 쓰는 그림 정리`를
            # 눌렀을 수 있다.
            for combo in restored:
                if combo.image_path and self._store.image_path(combo) is None:
                    combo.image_path = ""
        self.state.combos.extend(restored)
        self._last_removed = []
        self._last_removed_images = False
        self.save()
        return len(restored)

    # ------------------------------------------------------------ 기록 파일

    def export_archive(self, path: Path, *, app_version: str = "") -> ArchiveSummary:
        """지금 기록과 그림을 zip 하나에 담는다 (아레나 폴더는 그대로 둔다).

        Raises:
            ArchiveError: 쓸 수 없을 때.
        """
        self.save()  # 화면에서 방금 바꾼 것까지 담기게
        return export_archive(self._store, self.state, Path(path), app_version=app_version)

    def import_archive(self, path: Path) -> ArchiveSummary:
        """기록 파일을 읽어 **지금 기록을 갈아 끼운다.**

        부르기 전에 사용자 확인을 받아야 한다 — 지금 아레나 폴더의 내용이 사라진다.
        되돌리기 기록도 버린다 (지운 조합이 예전 기록의 것이라 되살릴 자리가 없다).

        Raises:
            ArchiveError: 읽거나 쓸 수 없을 때.
        """
        if self._running:
            raise ArchiveError("a batch is running")
        self.state = import_archive(self._store, Path(path))
        self.forget_undo()
        return ArchiveSummary(
            artists=len(self.state.artists),
            combos=len(self.state.combos),
            matches=self.state.total_matches,
            images=sum(1 for combo in self.state.combos if combo.image_path),
        )

    def forget_undo(self) -> None:
        """되돌리기 기록을 버린다 (폴더를 바꾸는 등 되돌릴 수 없게 됐을 때)."""
        self._last_removed = []
        self._last_removed_images = False

    def subscribe(self, callback: Callable[[ArenaEvent], None]) -> None:
        """아레나 이벤트 구독. 같은 콜백을 두 번 넣지 않는다."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[ArenaEvent], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def reload(self, base_dir: str | Path) -> None:
        """아레나 폴더가 바뀌었을 때 새 폴더의 데이터를 읽는다.

        생성이 도는 중에는 바꾸지 않는다 — 워커가 예전 폴더에 그림을 쓰고 있다.
        """
        if self._running:
            logger.info("arena folder change ignored while a batch is running")
            return
        self._store = ArenaStore(base_dir)
        self._store.ensure_dirs()
        self.state = self._store.load()
        self._store.prune_missing_images(self.state)
        self.forget_undo()  # 되돌릴 조합이 예전 폴더의 것이다

    # ------------------------------------------------------------------ 생성

    def needs_prefetch(self, threshold: int) -> bool:
        """싸울 수 있는 그림이 임계 아래로 내려갔고, 뽑을 조합이 남아 있는지."""
        return len(ready_combos(self.state)) < threshold and bool(pending_combos(self.state))

    def start(self, combos: list[Combo], spec: ArenaSpec) -> int:
        """`combos` 중 그림이 없는 것들의 그림을 뽑기 시작한다. 큐에 넣은 수를 돌려준다.

        `combos`의 원소는 `state.combos`에 들어 있는 **바로 그 객체**여야 한다 —
        완료 이벤트가 그 객체에 그림 경로를 적기 때문이다.
        """
        if self._running or self._generation.is_running:
            raise ArenaBusyError("another generation job is running")

        batch = [combo for combo in combos if not combo.has_image]
        if not batch:
            return 0

        self._store.ensure_dirs()
        self._batch = batch
        self._spec = spec
        self._seed = spec.seed if spec.seed > 0 else self._rng.randint(1, 2**32 - 1)
        self._completed = 0
        self._running = True

        job = GenerationJob(
            request=spec.request,
            count=len(batch),
            delay_seconds=spec.delay_seconds,
            save_dir=str(self._store.images_dir),
            filename_template=ARENA_FILENAME_TEMPLATE,
            image_format=spec.image_format,
            randomize_seed=False,  # 한 세션의 모든 조합이 같은 시드를 쓴다
            request_provider=self._request_for_index,
        )
        try:
            self._generation.start(job)
        except Exception:
            self._running = False
            self._batch = []
            raise
        self._emit(ArenaBatchStarted(total=len(batch), seed=self._seed))
        return len(batch)

    def stop(self) -> None:
        """진행 중인 묶음을 멈춘다. 이미 뽑은 그림은 그대로 남는다."""
        if self._running:
            self._generation.stop()

    # ------------------------------------------------------------------ 이벤트

    def handle_event(self, event: GenerationEvent) -> bool:
        """UI가 받은 생성 이벤트를 넘겨받는다. 아레나가 처리했으면 True.

        True를 돌려주면 메인 창은 그 이벤트를 무시해야 한다 — 아레나가 뽑은 습작이
        결과 미리보기나 갤러리, 생성 진행 표시에 끼어들면 안 된다.
        """
        if not self._running:
            return False
        if isinstance(event, ImageStarted):
            combo = self._combo_at(event.index)
            if combo is not None:
                self._emit(ArenaImageStarted(index=event.index, total=len(self._batch), combo_id=combo.id))
            return True
        if isinstance(event, ImageCompleted):
            self._bind_image(event)
            return True
        if isinstance(event, JobFinished):
            self._finish(event)
            return True
        if isinstance(event, ImageRetrying):
            self._emit(
                ArenaImageRetrying(
                    index=event.index,
                    total=len(self._batch),
                    reason=event.reason,
                    wait_seconds=event.wait_seconds,
                    attempt=event.attempt,
                )
            )
            return True
        if isinstance(event, WaitingNext):
            self._emit(
                ArenaWaitingNext(
                    next_index=event.next_index,
                    total=len(self._batch),
                    wait_seconds=event.wait_seconds,
                )
            )
            return True
        return True  # 잡 시작 등 나머지도 아레나 잡의 것이다

    # ------------------------------------------------------------------ 내부

    def _combo_at(self, index: int) -> Combo | None:
        """1부터 세는 잡 인덱스 → 이 묶음의 조합."""
        return self._batch[index - 1] if 1 <= index <= len(self._batch) else None

    def _request_for_index(self, index: int) -> GenerationRequest:
        """워커 스레드에서 불린다 — 위젯을 만지지 않는 순수 함수여야 한다."""
        combo = self._combo_at(index)
        if combo is None or self._spec is None:  # 일어나지 않아야 하지만 잡을 죽이진 않는다
            logger.warning("arena request for unknown index %s", index)
            return self._spec.request if self._spec is not None else GenerationRequest()
        return build_request(self._spec, combo, self._seed)

    def _bind_image(self, event: ImageCompleted) -> None:
        """방금 뽑은 그림을 조합에 붙이고 바로 저장한다.

        장마다 저장하는 이유는 도중에 앱이 죽어도 이미 쓴 크레딧이 날아가지 않게
        하기 위해서다 (`arena.json`은 작아서 비용이 무시할 만하다).
        """
        combo = self._combo_at(event.index)
        if combo is None:
            logger.warning("arena image for unknown index %s: %s", event.index, event.path)
            return
        combo.image_path = Path(event.path).name
        combo.seed = event.seed
        self._completed += 1
        self.save()
        self._emit(
            ArenaImageReady(
                index=event.index,
                total=len(self._batch),
                combo_id=combo.id,
                path=event.path,
            )
        )

    def _finish(self, event: JobFinished) -> None:
        self._running = False
        self._batch = []
        self._spec = None
        self.save()
        self._emit(
            ArenaBatchFinished(
                completed=self._completed,
                stopped=event.stopped,
                error=event.error,
                error_type=event.error_type,
            )
        )

    def _emit(self, event: ArenaEvent) -> None:
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # 구독자 하나의 예외가 아레나를 멈추면 안 된다
                logger.exception("arena event handler failed")
