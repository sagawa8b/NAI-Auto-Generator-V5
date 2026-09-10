"""아레나 그림체 자동 판독 — V4 참조 이미지와 후보들을 LLM(VLM)이 점수화한다 (Qt-free).

흐름:
    참조 이미지(V4 그림체) 여러 장을 한 번 로딩해 두고, 아레나의 **그림이 있는 조합**을
    한 장씩 VLM에 넣어 0~100 유사도 점수를 받는다. 결과는 사람 Elo와 **별개**로
    `Combo.judge_score`/`judge_reason`에 적히고, UI가 "LLM 추천 랭킹"으로 보여 준다.

왜 서비스가 스레드를 만들지 않나:
    `LMStudioPromptGenerator.score_style()`는 블로킹이라 워커 스레드(UI의 `_JudgeWorker`)
    에서 불러야 한다. 이 서비스는 그 워커 안에서 도는 **순수 루프**다 — 후보를 순서대로
    돌며 콜백으로 진행을 알리고, `should_cancel()`이 True면 즉시 멈춘다. NovelAI API를
    쓰지 않으므로 아레나 이미지 생성(`ArenaService`)의 직렬성 제약과는 무관하다.

왜 점수를 매 후보마다 저장하나:
    판독이 길어질 수 있어(후보 수 × 모델 응답 시간), 도중에 멈춰도 지금까지의 점수가
    남아야 한다. `arena.json`은 작아 매번 저장해도 부담이 없다 (`ArenaService`가 이미
    같은 이유로 매 대결마다 저장한다).
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.arena.combos import format_artist_block
from ..core.arena.finale import judge_leaderboard_combos
from ..core.arena.models import ArenaState, Combo, JudgeRun
from ..core.arena.store import ArenaStore
from ..core.llm.lmstudio_client import LMStudioError, LMStudioPromptGenerator
from ..core.llm.style_judge import StyleJudgeConfig, StyleScore

logger = logging.getLogger(__name__)

#: 취소 여부를 묻는 콜백. True면 판독 루프를 멈춘다.
CancelCheck = Callable[[], bool]


class StyleJudgeError(RuntimeError):
    """판독을 시작할 수 없는 상태 (참조 없음/후보 없음 등)."""


@dataclass(frozen=True)
class JudgeEvent:
    """판독 진행 이벤트의 베이스."""


@dataclass(frozen=True)
class JudgeStarted(JudgeEvent):
    total: int  # 판독할 후보 수
    references: int  # 실제로 쓰는 참조 장수 (상한 적용 후)


@dataclass(frozen=True)
class JudgeProgress(JudgeEvent):
    """후보 한 장의 판독이 끝났다."""

    index: int  # 1부터
    total: int
    combo_id: str
    score: int  # 0~100, 실패면 -1
    reason: str
    ok: bool  # False면 점수를 못 건졌다 (모델 응답 이상)


@dataclass(frozen=True)
class JudgeFinished(JudgeEvent):
    completed: int  # 성공적으로 점수를 받은 후보 수
    failed: int  # 응답은 왔으나 점수를 못 건진 수
    stopped: bool = False  # 사용자가 중간에 멈췄나
    error: str | None = None  # 연결/모델 오류로 통째로 실패했을 때의 메시지
    error_key: str | None = None  # UI가 i18n으로 옮길 오류 종류 (예외 클래스명)
    #: 서버가 실제로 쓴 모델 이름 (한 장도 못 매겼으면 빈 문자열). UI가 "무슨 모델로
    #: 매겼는지"를 알려 줄 때 쓴다.
    model: str = ""


def load_reference_images(paths: list[str], limit: int) -> list[bytes]:
    """참조 이미지 파일들을 바이트로 읽는다. 읽을 수 있는 것만, 상한까지.

    깨진/사라진 파일은 조용히 건너뛴다 — 참조가 하나라도 살아 있으면 판독은 된다.
    상한(`limit`)은 컨텍스트 초과를 막기 위한 것이라 여기서 먼저 자른다.
    """
    images: list[bytes] = []
    cap = limit if limit > 0 else len(paths)
    for raw in paths:
        if len(images) >= cap:
            break
        path = Path(raw)
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.warning("cannot read reference image %s: %s", path, e)
            continue
        if data:
            images.append(data)
    return images


def candidates_to_judge(state: ArenaState, only_unscored: bool = False) -> list[Combo]:
    """판독 대상 후보 — 그림이 있는 조합. `only_unscored`면 아직 점수가 없는 것만.

    다시 돌릴 때 이미 점수를 받은 것을 건너뛰고 싶을 때 `only_unscored=True`를 쓴다.
    """
    combos = [combo for combo in state.combos if combo.has_image]
    if only_unscored:
        combos = [combo for combo in combos if not combo.has_judge_score]
    return combos


class StyleJudgeService:
    """참조 이미지와 후보들을 받아 순차 판독하는 계층.

    상태(`ArenaState`)와 저장(`ArenaStore`)은 `ArenaService`와 같은 것을 공유한다 —
    판독 점수도 결국 같은 `arena.json`에 남기 때문이다. 이 서비스는 상태를 **판독
    결과로만** 고친다 (조합 추가/삭제는 `ArenaService`의 몫).
    """

    def __init__(
        self,
        store: ArenaStore,
        state: ArenaState,
        generator: LMStudioPromptGenerator | None = None,
    ) -> None:
        self._store = store
        self._state = state
        self._generator = generator or LMStudioPromptGenerator()

    @property
    def state(self) -> ArenaState:
        return self._state

    def set_state(self, state: ArenaState) -> None:
        """아레나 폴더가 바뀌어 상태가 새로 로드되면 갈아 끼운다."""
        self._state = state

    def run(
        self,
        references: list[bytes],
        combos: list[Combo],
        config: StyleJudgeConfig,
        on_event: Callable[[JudgeEvent], None] = lambda event: None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        """후보들을 순서대로 판독한다. 워커 스레드에서 호출해야 한다 (블로킹).

        Parameters
        ----------
        references : list[bytes]
            목표 그림체 참조 이미지 바이트들 (이미 상한 적용·로딩된 상태).
        combos : list[Combo]
            판독할 후보들. `state.combos`에 들어 있는 **바로 그 객체**여야 한다 —
            점수를 그 객체에 적기 때문이다.
        config : StyleJudgeConfig
            연결·모델·타임아웃·참조 상한.
        on_event : Callable[[JudgeEvent], None]
            진행/완료 이벤트를 받는 콜백 (UI가 Qt 시그널로 옮긴다).
        should_cancel : Callable[[], bool] | None
            True를 돌려주면 다음 후보로 넘어가지 않고 멈춘다.

        Raises
        ------
        StyleJudgeError
            참조나 후보가 비어 있어 시작할 수 없을 때.
        """
        cancelled = should_cancel or (lambda: False)
        refs = [ref for ref in references if ref]
        if not refs:
            raise StyleJudgeError("no reference images to judge against")
        if not combos:
            raise StyleJudgeError("no candidates with images to judge")

        total = len(combos)
        on_event(JudgeStarted(total=total, references=len(refs)))

        completed = 0
        failed = 0
        # 서버가 실제로 고른 모델 이름. 첫 응답에서 알게 되므로 루프를 돌며 채운다.
        model = ""
        for i, combo in enumerate(combos, start=1):
            if cancelled():
                self._record_run(config, model=model, references=len(refs), scored=completed, failed=failed)
                on_event(JudgeFinished(completed=completed, failed=failed, stopped=True, model=model))
                return

            candidate = self._load_candidate(combo)
            if candidate is None:
                # 그림 파일이 사라졌다 — 실패로 세고 다음으로.
                failed += 1
                on_event(
                    JudgeProgress(index=i, total=total, combo_id=combo.id, score=-1, reason="", ok=False)
                )
                continue

            try:
                result = self._generator.score_style(refs, candidate, config, cancelled)
            except LMStudioError as e:
                # 연결/모델/타임아웃 등은 후보 하나가 아니라 전체를 막는 문제라 중단한다.
                logger.warning("style judging aborted: %s", e)
                self._record_run(config, model=model, references=len(refs), scored=completed, failed=failed)
                on_event(
                    JudgeFinished(
                        completed=completed,
                        failed=failed,
                        error=str(e),
                        error_key=type(e).__name__,
                        model=model,
                    )
                )
                return
            except Exception as e:  # noqa: BLE001 - 예상 못한 오류도 판독을 통째로 죽이지 않는다
                logger.exception("unexpected error while judging candidate %s", combo.id)
                self._record_run(config, model=model, references=len(refs), scored=completed, failed=failed)
                on_event(
                    JudgeFinished(
                        completed=completed,
                        failed=failed,
                        error=str(e),
                        error_key="Exception",
                        model=model,
                    )
                )
                return

            model = result.model or model
            self._apply_score(combo, result)
            if result.ok:
                completed += 1
            else:
                failed += 1
            on_event(
                JudgeProgress(
                    index=i,
                    total=total,
                    combo_id=combo.id,
                    score=result.score if result.ok else -1,
                    reason=result.reason,
                    ok=result.ok,
                )
            )

        self._record_run(config, model=model, references=len(refs), scored=completed, failed=failed)
        on_event(JudgeFinished(completed=completed, failed=failed, model=model))

    # ------------------------------------------------------------------ 내부

    def _load_candidate(self, combo: Combo) -> bytes | None:
        """후보 조합의 그림을 바이트로 읽는다. 파일이 없으면 None."""
        path = self._store.image_path(combo)
        if path is None:
            logger.warning("candidate %s has no image file", combo.id)
            return None
        try:
            return path.read_bytes()
        except OSError as e:
            logger.warning("cannot read candidate image %s: %s", path, e)
            return None

    def _apply_score(self, combo: Combo, result: StyleScore) -> None:
        """판독 결과를 조합에 적고 바로 저장한다.

        실패(`ok=False`)면 점수는 -1(모름)로 두되 근거(원문 일부)는 남긴다 — 왜 실패했는지
        UI가 보여 줄 수 있게. 매번 저장하는 이유는 도중에 멈춰도 결과를 잃지 않기 위해서다.

        **모델 이름을 함께 적는다.** 모델을 바꿔 가며 견주려면 점수 옆에 누가 매겼는지가
        남아야 한다. 점수를 못 건진 줄에도 적는다 — "그 모델이 이 후보에서 실패했다"는
        것도 비교에 쓰이는 정보다.
        """
        combo.judge_score = result.score if result.ok else -1
        combo.judge_reason = result.reason
        combo.judge_model = result.model
        self._store.save(self._state)

    def _record_run(
        self,
        config: StyleJudgeConfig,
        *,
        model: str,
        references: int,
        scored: int,
        failed: int,
    ) -> None:
        """이번 판독이 어떤 조건으로 돌았는지 상태에 남긴다 (`arena.json`에 함께 저장).

        점수만으로는 "참조 3장으로 매긴 점수"와 "5장으로 매긴 점수"를 구분할 수 없다.
        모델 간 비교의 기준선이므로 한 회차가 끝날 때마다 갈아 끼운다.
        """
        self._state.judge_run = JudgeRun(
            model=model,
            host=config.host,
            timeout=config.timeout,
            references=references,
            max_references=config.max_reference_images,
            custom_prompt=bool((config.system_prompt or "").strip()),
            scored=scored,
            failed=failed,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._store.save(self._state)


def apply_favorite_top_n(state: ArenaState, top_n: int) -> int:
    """판독 점수 상위 N개 조합을 즐겨찾기로 표시한다. 새로 표시한 개수를 돌려준다.

    후속 액션 중 하나다 — 판독이 끝난 뒤 "가장 V4스러운 것들"을 한 번에 즐겨찾기로
    올려, 사람이 월드컵의 즐겨찾기 리그에서 그것들만 더 비교할 수 있게 한다.
    이미 즐겨찾기인 것은 그대로 둔다 (새로 켠 것만 센다).
    """
    if top_n <= 0:
        return 0
    scored = [combo for combo in state.combos if combo.has_judge_score]
    scored.sort(key=lambda c: c.judge_score, reverse=True)
    newly = 0
    for combo in scored[:top_n]:
        if not combo.favorite:
            combo.favorite = True
            newly += 1
    return newly


def judge_leaderboard(state: ArenaState) -> list[Combo]:
    """판독 점수가 있는 조합을 점수 내림차순으로. UI 랭킹 표가 그대로 쓴다.

    정렬 로직은 core(`finale.judge_leaderboard_combos`)가 소유한다 — LLM 결산과 같은
    순서를 쓰도록 한 곳에 모은다.
    """
    return judge_leaderboard_combos(state.combos)


#: 결과 CSV의 헤더. UI가 표에 보여 주는 것과 같은 열이다.
JUDGE_CSV_HEADER = ("rank", "score", "combo", "reason")

#: 판독 조건까지 담을 때의 헤더 — `model` 한 칸이 더 붙는다. 여러 모델의 CSV를 이어
#: 붙여 놓고 모델별로 묶어 볼 수 있게, 조건이 아니라 **줄마다** 모델을 적는다.
JUDGE_CSV_HEADER_WITH_RUN = ("rank", "score", "model", "combo", "reason")

#: 조건 구역의 머리글. 데이터와 빈 줄 하나로 떨어뜨려 둔다.
JUDGE_CSV_RUN_HEADER = ("setting", "value")


def judge_run_rows(run: JudgeRun | None) -> list[tuple[str, str]]:
    """판독 조건을 `(항목, 값)` 줄들로. 조건이 없으면 빈 목록.

    키는 번역하지 않는다 — 스프레드시트에서 여러 벌을 이어 붙여 비교하는 자리라,
    언어를 바꿔 가며 뽑은 CSV가 서로 다른 열 이름을 갖게 하면 안 된다.
    """
    if run is None:
        return []
    return [
        ("model", run.model),
        ("host", run.host),
        ("timeout_seconds", f"{run.timeout:g}"),
        ("reference_images", str(run.references)),
        ("max_reference_images", str(run.max_references)),
        ("custom_system_prompt", "yes" if run.custom_prompt else "no"),
        ("scored", str(run.scored)),
        ("failed", str(run.failed)),
        ("finished_at", run.finished_at),
    ]


def judge_results_to_csv(state: ArenaState, use_prefix: bool = True, include_run: bool = False) -> str:
    """판독 랭킹을 CSV 텍스트로. 표에 보이는 순서(점수 내림차순) 그대로.

    사람이 읽거나 스프레드시트로 옮기는 **결과물**이다 — 아레나 기록 내보내기(zip)와
    목적이 다르다. 조합·근거에 쉼표/따옴표가 섞여도 표준 CSV 규칙으로 감싼다.

    `include_run`을 켜면 **어떤 모델이 어떤 조건으로 매겼는지**가 함께 나간다: 줄마다
    `model` 칸이 붙고, 데이터 아래에 빈 줄 하나를 두고 조건 구역이 따라온다. 모델을
    바꿔 가며 판독해 성능을 견줄 때 필요한 정보다. 끄면 예전과 똑같은 네 칸이다.

    `csv` 모듈은 기본 개행을 `\\r\\n`으로 쓴다. `io.StringIO(newline="")`로 받아
    모듈이 넣는 개행을 그대로 두면, 어느 OS에서 열어도 표준 CSV로 읽힌다.
    """
    ranked = judge_leaderboard_combos(state.combos)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(JUDGE_CSV_HEADER_WITH_RUN if include_run else JUDGE_CSV_HEADER)
    for rank, combo in enumerate(ranked, start=1):
        block = format_artist_block(combo.slots, use_prefix)
        if include_run:
            writer.writerow([rank, combo.judge_score, combo.judge_model, block, combo.judge_reason])
        else:
            writer.writerow([rank, combo.judge_score, block, combo.judge_reason])

    rows = judge_run_rows(state.judge_run) if include_run else []
    if rows:
        writer.writerow([])  # 데이터와 조건 구역을 빈 줄로 떼어 놓는다
        writer.writerow(JUDGE_CSV_RUN_HEADER)
        for key, value in rows:
            writer.writerow([key, value])
    return buffer.getvalue()


def clear_judge_scores(state: ArenaState) -> int:
    """모든 조합의 판독 점수·근거를 초기화한다. 지운 개수를 돌려준다.

    사람 Elo·전적·즐겨찾기는 건드리지 않는다 — 판독은 그것들과 별개이기 때문이다.
    참조 이미지 등록 목록도 그대로 둔다 (다시 판독할 때 쓴다). 되돌리기가 없으므로
    UI는 확인을 받은 뒤 부른다.
    """
    cleared = 0
    for combo in state.combos:
        if combo.has_judge_score or combo.judge_reason:
            combo.judge_score = -1
            combo.judge_reason = ""
            combo.judge_model = ""
            cleared += 1
    # 지워진 점수의 조건만 남아 있으면 거짓말이 된다 — 함께 지운다.
    state.judge_run = None
    return cleared
