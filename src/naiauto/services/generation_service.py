"""생성 오케스트레이션 — 단일·배치 생성 파이프라인 (Qt-free).

설계 (PLANNING.md M1):
  - ThreadPoolExecutor(max_workers=1): NovelAI는 직렬 생성이 규칙이므로
    동시 생성 차단이 구조적으로 보장된다.
  - UI는 시작 시점에 불변 GenerationJob을 넘기고 위젯을 더 만지지 않는다.
  - 중지: threading.Event — 대기(딜레이/백오프)도 stop-aware.
  - 에러 정책:
      RateLimitError        → Retry-After(없으면 30s) 대기 후 같은 이미지 재시도 (최대 5회)
      ServerBusy/Network    → 5s→10s→20s 백오프 재시도 (최대 3회), 초과 시 잡 중단
      그 외 NAIError        → 즉시 잡 중단 (Anlas 부족, payload 거부, 인증 실패 등)
  - 와일드카드: 이미지 1장 = 1사이클 (snapshot → apply → advance).
"""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from ..core.api.client import NAIClient
from ..core.api.errors import NAIError, NetworkError, RateLimitError, ServerBusyError
from ..core.api.models import CharacterCaption, GenerationRequest
from ..core.artist_combos import ArtistComboEngine
from ..core.credit_estimator import CreditObservation
from ..core.metadata.save import save_raw_png
from ..core.prompt_choices import resolve_prompt_choices
from ..core.wildcards.applier import WildcardApplier
from .events import (
    GenerationEvent,
    ImageCompleted,
    ImageRetrying,
    ImageStarted,
    JobFinished,
    JobStarted,
    WaitingNext,
)

logger = logging.getLogger(__name__)

MAX_RATE_LIMIT_RETRIES = 5
MAX_TRANSIENT_RETRIES = 3
DEFAULT_RATE_LIMIT_WAIT = 30.0
TRANSIENT_BACKOFF = (5.0, 10.0, 20.0)


class _StopRequested(Exception):
    """내부 제어 흐름용 — 서비스 밖으로 나가지 않는다."""


class FixedSeedBatchError(Exception):
    """고정 시드로 2장 이상을 요청했다 — 같은 그림만 반복되고 Anlas만 소모된다."""


@dataclass(frozen=True)
class GenerationJob:
    """배치 1회 실행의 불변 명세. request의 prompt/negative_prompt는
    와일드카드 전개 전 템플릿이다."""

    request: GenerationRequest
    count: int = 1  # 0 = 무한 (중지 버튼으로만 종료)
    delay_seconds: float = 3.0  # 요청 간 최소 간격
    save_dir: str = "results"
    filename_template: str = "{datetime}_{seed}"
    image_format: str = "png"  # "png" | "webp" — core.metadata.save.IMAGE_FORMATS
    prompt_word_limit: int = 20  # {prompt} 토큰에 남길 단어 수 (Req 3.5)
    character_word_limit: int = 20  # {character} 토큰에 남길 단어 수 (Req 3.6)
    randomize_seed: bool = True  # True: 매 장 새 시드 / False: request.seed 고정
    measure_credit: bool = False  # 매 장 후 V5 크레딧/Anlas를 로그에 기록 (요청 1회 추가)
    randomize_resolution: bool = False  # True: 매 장 resolution_choices 중 하나로 해상도 변경
    resolution_choices: tuple[tuple[int, int], ...] = ()  # Aspect별 대표 해상도 (2개 미만이면 무시)


class GenerationService:
    def __init__(
        self,
        client: NAIClient,
        wildcards: WildcardApplier | None = None,
        artist_combos: ArtistComboEngine | None = None,
        on_event: Callable[[GenerationEvent], None] = lambda e: None,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._wildcards = wildcards
        self._artist_combos = artist_combos
        self._on_event = on_event
        self._rng = rng or random.Random()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="naiauto-gen")
        self._stop = threading.Event()
        self._future: Future | None = None
        # `is_running`의 근거. `_future.done()`에 기대면 경합이 생긴다 — 워커 스레드는
        # `_run()` 안에서 `JobFinished`를 emit한 *뒤에도* 아직 함수가 안 끝나 future가
        # done 처리되기 전이라, 그 이벤트를 받은 GUI 스레드가 곧바로 다음 잡을 `start()`하면
        # (세팅별 연속 생성처럼) `is_running`이 여전히 True로 보여 RuntimeError가 난다.
        # 그래서 `JobFinished`를 emit하기 *직전에* 이 플래그부터 내린다 (`_emit()` 참고).
        self._running = False
        self._last_probe: tuple[int | None, int | None] | None = None
        self._last_probe_at: float = 0.0
        # GUI 스레드 → 워커 스레드로 넘어가는 유일한 값(_stop 제외): 배치 도중 해상도 패널에서
        # 크기를 바꾸면 다음 이미지부터 반영하기 위한 실시간 오버라이드 (Lock으로 보호).
        self._live_resolution_lock = threading.Lock()
        self._live_resolution: tuple[int, int] | None = None
        self._live_resolution_choices: tuple[tuple[int, int], ...] = ()
        # 같은 이유로: 배치 도중 프롬프트/캐릭터 프롬프트를 고치면 다음 이미지부터 반영한다.
        self._live_prompt_lock = threading.Lock()
        self._live_prompt: tuple[str, str, tuple[CharacterCaption, ...], bool] | None = None

    def reload_artist_combos(self, combos_dir: str) -> None:
        """아티스트 조합 폴더를 다시 읽는다 (옵션에서 경로를 바꿨을 때).

        실행 중인 워커가 옛 엔진을 계속 쓰도록 **새 엔진을 만들어 참조만 바꾼다** —
        생성 도중 경로를 바꿔도 진행 중인 잡의 치환 결과가 흔들리지 않는다.
        """
        engine = ArtistComboEngine(combos_dir)
        engine.load()
        self._artist_combos = engine

    # ── 제어 ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, job: GenerationJob) -> Future:
        """잡 시작. 이미 실행 중이면 RuntimeError (직렬 생성 강제)."""
        if self.is_running:
            raise RuntimeError("a generation job is already running")
        self._running = True
        self._stop.clear()
        with self._live_resolution_lock:
            self._live_resolution = None
            self._live_resolution_choices = ()
        with self._live_prompt_lock:
            self._live_prompt = None
        self._future = self._executor.submit(self._run, job)
        return self._future

    def stop(self) -> None:
        """현재 잡에 중지 요청. 진행 중인 HTTP 요청은 완료를 기다린다."""
        self._stop.set()

    def set_live_resolution(self, width: int, height: int, choices: tuple[tuple[int, int], ...] = ()) -> None:
        """해상도 패널이 바뀔 때 GUI 스레드에서 호출 — 다음 `_prepare_request()`부터 반영된다.

        진행 중인 이미지에는 영향이 없다 (다음 이미지의 요청을 만들 때만 읽는다).
        """
        with self._live_resolution_lock:
            self._live_resolution = (width, height)
            self._live_resolution_choices = choices

    def set_live_prompt(
        self,
        prompt: str,
        negative_prompt: str,
        characters: tuple[CharacterCaption, ...],
        use_coords: bool,
    ) -> None:
        """프롬프트/캐릭터 프롬프트가 바뀔 때 GUI 스레드에서 호출 — 다음 `_prepare_request()`부터
        반영된다 (`set_live_resolution`과 같은 패턴). 진행 중인 이미지에는 영향이 없다.

        `use_coords`도 함께 넘긴다 — 캐릭터별 좌표는 이미 그 값을 기준으로 굳어 있으므로
        (`captions()`), 요청의 최상위 플래그가 어긋나면 payload가 일관되지 않는다."""
        with self._live_prompt_lock:
            self._live_prompt = (prompt, negative_prompt, characters, use_coords)

    def shutdown(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── 실행 루프 ─────────────────────────────────────────

    def _emit(self, event: GenerationEvent) -> None:
        if isinstance(event, JobFinished):
            # 콜백을 부르기 전에 내려야 한다 — 콜백이 (세팅별 연속 생성처럼) 그 자리에서
            # 바로 `start()`를 부를 수 있고, 그 시점엔 `is_running`이 이미 False여야 한다.
            self._running = False
        try:
            self._on_event(event)
        except Exception:
            logger.exception("event callback failed")

    def _wait(self, seconds: float, next_index: int | None = None) -> None:
        """stop-aware countdown wait. Emits WaitingNext per second when next_index is provided."""
        if seconds <= 0:
            return
        if self._stop.is_set():
            raise _StopRequested

        # If no next_index, this is a simple stop-aware wait (e.g. retry backoff)
        if next_index is None:
            if self._stop.wait(timeout=seconds):
                raise _StopRequested
            return

        if seconds < 1.0:
            # Sub-second delay: single emission, then wait
            self._emit(WaitingNext(next_index=next_index, wait_seconds=seconds))
            if self._stop.wait(timeout=seconds):
                raise _StopRequested
            return

        remaining = seconds
        # Initial emission with full delay
        self._emit(WaitingNext(next_index=next_index, wait_seconds=remaining))

        while remaining >= 1.0:
            if self._stop.wait(timeout=1.0):
                raise _StopRequested
            remaining -= 1.0
            if remaining >= 1.0:
                self._emit(WaitingNext(next_index=next_index, wait_seconds=remaining))

        # Fractional remainder — no emission
        if remaining > 0:
            if self._stop.wait(timeout=remaining):
                raise _StopRequested

    def _run(self, job: GenerationJob) -> None:
        self._emit(JobStarted(total=job.count))
        self._last_probe = None
        self._last_probe_at = 0.0
        self._credit_observations: list[CreditObservation] = []
        completed = 0
        index = 0
        try:
            # 고정 시드 + 연속 생성 = 같은 이미지 반복. 시작 전에 막는다.
            if not job.randomize_seed and job.count != 1:
                raise FixedSeedBatchError("batch generation requires a random seed")
            while job.count == 0 or index < job.count:
                if self._stop.is_set():
                    raise _StopRequested
                index += 1

                req = self._prepare_request(job)
                self._emit(ImageStarted(index=index, seed=req.seed, prompt=req.prompt))

                result = self._generate_with_policy(req, index)

                # 첫 캐릭터 프롬프트 추출은 서비스 책임 — save.py는 CharacterCaption을
                # 알 필요가 없다 (Req 3.3).
                path = save_raw_png(
                    result.raw_bytes,
                    job.save_dir,
                    template=job.filename_template,
                    context={
                        "seed": req.seed,
                        "model": req.model,
                        "prompt": req.prompt,
                        "negative_prompt": req.negative_prompt,
                        "character": req.characters[0].prompt if req.characters else "",
                    },
                    prompt_word_limit=job.prompt_word_limit,
                    character_word_limit=job.character_word_limit,
                    image_format=job.image_format,
                )
                completed += 1
                self._emit(
                    ImageCompleted(
                        index=index,
                        path=str(path),
                        size_bytes=len(result.raw_bytes),
                        seed=req.seed,
                    )
                )

                if job.measure_credit:
                    self._log_credit(index)

                if job.count == 0 or index < job.count:
                    self._wait(job.delay_seconds, next_index=index + 1)

            obs = tuple(self._credit_observations) if job.measure_credit else ()
            self._emit(JobFinished(completed=completed, credit_observations=obs))
        except _StopRequested:
            obs = tuple(self._credit_observations) if job.measure_credit else ()
            self._emit(JobFinished(completed=completed, stopped=True, credit_observations=obs))
        except FixedSeedBatchError as e:
            logger.info("job refused: %s", e)
            self._emit(JobFinished(completed=completed, error=str(e), error_type=type(e).__name__))
        except NAIError as e:
            logger.warning("job aborted: %s", e)
            obs = tuple(self._credit_observations) if job.measure_credit else ()
            self._emit(
                JobFinished(
                    completed=completed, error=str(e), error_type=type(e).__name__, credit_observations=obs
                )
            )
        except Exception as e:  # 마지막 방어선 — 워커 스레드 예외는 조용히 사라지면 안 됨
            logger.exception("unexpected error in generation job")
            self._emit(JobFinished(completed=completed, error=str(e), error_type=type(e).__name__))

    def _log_credit(self, index: int) -> None:
        """생성 1장 후 V5 크레딧/Anlas 관측값을 로그에 남긴다 (소모량 측정용).

        `usage.percent`는 정수라 1장으로는 잘 움직이지 않는다. 그래서 파생값을
        추정하지 않고 **관측 원값을 그대로** 찍는다 — 배치를 한 번 돌린 뒤
        `v5-credit` 줄만 모으면 장당 소모량을 직접 계산할 수 있다.
        `next_percent_in`은 시간이 지나면 저절로 줄어들므로, 소비량을 보려면
        elapsed와 함께 봐야 한다 (소비하면 그만큼 다시 늘어난다).

        측정 때문에 생성이 멈추면 안 되므로 어떤 예외도 삼킨다.
        """
        try:
            info = self._client.get_anlas()
        except Exception as e:  # 측정은 부가 기능 — 잡을 방해하지 않는다
            logger.debug("credit probe failed: %s", e)
            return

        usage = info.get("usage")
        now = time.monotonic()
        elapsed = now - self._last_probe_at if self._last_probe_at else 0.0
        self._last_probe_at = now

        percent = getattr(usage, "percent", None)
        next_in = getattr(usage, "seconds_to_next_percent", None)
        anlas = info.get("total")
        previous = self._last_probe
        deltas = ""
        if previous is not None:
            d_percent = None if percent is None else percent - previous[0]
            d_anlas = None if anlas is None else anlas - previous[1]
            deltas = f" dpercent={d_percent} danlas={d_anlas}"
        self._last_probe = (percent, anlas)

        # Collect CreditObservation for batch cost computation
        if percent is not None:
            self._credit_observations.append(CreditObservation(index=index, percent=percent, timestamp=now))

        logger.info(
            "v5-credit index=%s percent=%s next_percent_in=%s anlas=%s elapsed=%.1f%s",
            index,
            percent,
            next_in,
            anlas,
            elapsed,
            deltas,
        )

    def _prepare_request(self, job: GenerationJob) -> GenerationRequest:
        """이미지 1장분 불변 요청 파생: 와일드카드 1사이클 + 아티스트 콤보 1사이클 + 시드 결정."""
        req = job.request
        with self._live_prompt_lock:
            live_prompt = self._live_prompt
        if live_prompt is not None:
            prompt, negative, characters, use_coords = live_prompt
            req = dataclasses.replace(
                req, prompt=prompt, negative_prompt=negative, characters=characters, use_coords=use_coords
            )
        if self._wildcards is not None:
            self._wildcards.create_index_snapshot()
            prompt = self._wildcards.apply_wildcards_with_snapshot(req.prompt)
            negative = self._wildcards.apply_wildcards_with_snapshot(req.negative_prompt)
            characters = tuple(
                dataclasses.replace(
                    c,
                    prompt=self._wildcards.apply_wildcards_with_snapshot(c.prompt),
                    uc=self._wildcards.apply_wildcards_with_snapshot(c.uc),
                )
                for c in req.characters
            )
            self._wildcards.advance_loopcard_indices()
            req = dataclasses.replace(req, prompt=prompt, negative_prompt=negative, characters=characters)
        if self._artist_combos is not None:
            self._artist_combos.create_index_snapshot()
            prompt = self._artist_combos.apply(req.prompt)
            negative = self._artist_combos.apply(req.negative_prompt)
            characters = tuple(
                dataclasses.replace(
                    c,
                    prompt=self._artist_combos.apply(c.prompt),
                    uc=self._artist_combos.apply(c.uc),
                )
                for c in req.characters
            )
            self._artist_combos.advance_loopcard_indices()
            req = dataclasses.replace(req, prompt=prompt, negative_prompt=negative, characters=characters)

        prompt = resolve_prompt_choices(req.prompt, self._rng)
        negative = resolve_prompt_choices(req.negative_prompt, self._rng)
        characters = tuple(
            dataclasses.replace(
                c,
                prompt=resolve_prompt_choices(c.prompt, self._rng),
                uc=resolve_prompt_choices(c.uc, self._rng),
            )
            for c in req.characters
        )
        req = dataclasses.replace(req, prompt=prompt, negative_prompt=negative, characters=characters)

        with self._live_resolution_lock:
            live_size = self._live_resolution
            live_choices = self._live_resolution_choices
        choices = live_choices or job.resolution_choices
        if job.randomize_resolution and len(choices) >= 2:
            width, height = self._rng.choice(choices)
            req = dataclasses.replace(req, width=width, height=height)
        elif live_size is not None:
            req = dataclasses.replace(req, width=live_size[0], height=live_size[1])

        if job.randomize_seed:
            req = req.with_seed(self._rng.randint(1, 2**32 - 1))
        return req

    def _generate_with_policy(self, req: GenerationRequest, index: int):
        rate_limit_retries = 0
        transient_retries = 0
        while True:
            if self._stop.is_set():
                raise _StopRequested
            try:
                return self._client.generate(req)
            except RateLimitError as e:
                rate_limit_retries += 1
                if rate_limit_retries > MAX_RATE_LIMIT_RETRIES:
                    raise
                wait = e.retry_after if e.retry_after is not None else DEFAULT_RATE_LIMIT_WAIT
                self._emit(
                    ImageRetrying(
                        index=index,
                        reason="rate_limit",
                        wait_seconds=wait,
                        attempt=rate_limit_retries,
                    )
                )
                self._wait(wait)
            except (ServerBusyError, NetworkError) as e:
                transient_retries += 1
                if transient_retries > MAX_TRANSIENT_RETRIES:
                    raise
                wait = TRANSIENT_BACKOFF[min(transient_retries - 1, len(TRANSIENT_BACKOFF) - 1)]
                self._emit(
                    ImageRetrying(
                        index=index,
                        reason=type(e).__name__,
                        wait_seconds=wait,
                        attempt=transient_retries,
                    )
                )
                self._wait(wait)
