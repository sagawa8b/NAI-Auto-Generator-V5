"""옵션 값 검증 — Qt를 모르는 순수 함수 (Req 1.7, 3.10, 3.11, 4.3, 4.4, 5.8).

Options_Dialog는 저장 시 커밋된 드래프트를 `validate_options`에 넘기고, 첫 이슈의
`page`로 화면을 전환한 뒤 `field_key`/`message_key`를 번역해 경고 박스를 만든다.
검증 규칙과 "그 규칙이 사는 화면"을 한 데이터 구조로 묶어 두면, 새 필드를 추가할 때
UI 쪽에 필드→페이지 매핑을 따로 유지하지 않아도 된다.

위젯 스핀박스 범위가 이미 값을 막지만, 손으로 편집한 settings.json도 잡히도록
검증은 core에서 한 번 더 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..metadata.save import has_known_token
from ..resolution_catalog import DIMENSION_STEP, MAX_DIMENSION, MIN_DIMENSION, is_valid_dimension
from .schema import AppSettings

__all__ = [
    "BATCH_COUNT_RANGE",
    "BATCH_DELAY_RANGE",
    "LLM_TIMEOUT_RANGE",
    "QUICK_COUNT_RANGE",
    "OptionIssue",
    "PAGE_FILENAME",
    "PAGE_GENERATION",
    "PAGE_LLM",
    "PAGE_RESOLUTION",
    "WORD_LIMIT_RANGE",
    "has_known_token",
    "validate_options",
]

# Options_Dialog의 NAV_ORDER와 같은 문자열. core가 ui를 import하지 않도록 여기서 정의한다.
PAGE_FILENAME = "filename"
PAGE_GENERATION = "generation"
PAGE_RESOLUTION = "resolution"
PAGE_LLM = "llm"

WORD_LIMIT_RANGE: tuple[int, int] = (1, 100)
BATCH_COUNT_RANGE: tuple[int, int] = (0, 99999)
BATCH_DELAY_RANGE: tuple[float, float] = (0.0, 3600.0)
#: 퀵 매수 버튼 값. 0(무한)은 버튼으로 의미가 없어 1부터 받는다.
QUICK_COUNT_RANGE: tuple[int, int] = (1, 99999)
#: LM Studio 응답 타임아웃(초). 0은 "무제한"이라 허용한다 (상한은 넉넉히).
LLM_TIMEOUT_RANGE: tuple[float, float] = (0.0, 3600.0)

# 알려진 토큰 판정은 `core.metadata.save.has_known_token`이 유일한 구현이다 (TOKEN_NAMES와 같은
# 목록을 두 번 적지 않기 위해 그대로 재수출한다). save.py는 stdlib만 쓰므로 Qt-free가 유지된다.


@dataclass(frozen=True)
class OptionIssue:
    """검증 위반 1건. UI는 페이지 전환과 번역만 담당한다."""

    page: str  # NAV_ORDER의 키 — 어느 Options_Page로 전환할지
    field_key: str  # 항목 이름 i18n 키
    message_key: str  # 메시지 i18n 키
    args: tuple[object, ...] = ()  # 허용 범위 등 메시지 포맷 인자
    field_args: tuple[object, ...] = ()  # 항목 이름 포맷 인자 (예: 퀵 버튼 번호)


def validate_options(settings: AppSettings) -> tuple[OptionIssue, ...]:
    """옵션 다이얼로그가 다루는 모든 값을 검사한다. 순서는 NAV_ORDER를 따른다.

    filename 페이지: 템플릿 비어 있음 / 토큰 없음 / 단어 수 제한 1–100
    generation 페이지: batch.count 0–99999 / batch.delay_seconds 0–3600 /
                       batch.quick_counts 각 1–99999
    resolution 페이지: 활성 커스텀 행의 width/height 64–2048 & 64의 배수
    """
    issues: list[OptionIssue] = []
    issues += _validate_filename(settings)
    issues += _validate_generation(settings)
    issues += _validate_resolution(settings)
    issues += _validate_llm(settings)
    return tuple(issues)


def _validate_llm(settings: AppSettings) -> list[OptionIssue]:
    """LM Studio 응답 타임아웃 범위만 검사한다 (host/model은 실행 시점에 연결로 확인)."""
    low, high = LLM_TIMEOUT_RANGE
    if not low <= settings.lmstudio.timeout_seconds <= high:
        return [OptionIssue(PAGE_LLM, "options.llm_timeout", "options.err_range", (low, high))]
    return []


def _validate_filename(settings: AppSettings) -> list[OptionIssue]:
    issues: list[OptionIssue] = []
    template = settings.filename_template
    if not template.strip():
        issues.append(OptionIssue(PAGE_FILENAME, "options.filename_template", "options.err_template_empty"))
    elif not has_known_token(template):
        issues.append(
            OptionIssue(PAGE_FILENAME, "options.filename_template", "options.err_template_no_token")
        )

    low, high = WORD_LIMIT_RANGE
    for field_key, value in (
        ("options.prompt_word_limit", settings.prompt_word_limit),
        ("options.character_word_limit", settings.character_word_limit),
    ):
        if not low <= value <= high:
            issues.append(OptionIssue(PAGE_FILENAME, field_key, "options.err_range", (low, high)))
    return issues


def _validate_generation(settings: AppSettings) -> list[OptionIssue]:
    issues: list[OptionIssue] = []
    count_low, count_high = BATCH_COUNT_RANGE
    if not count_low <= settings.batch.count <= count_high:
        issues.append(
            OptionIssue(PAGE_GENERATION, "options.batch_count", "options.err_range", (count_low, count_high))
        )
    delay_low, delay_high = BATCH_DELAY_RANGE
    if not delay_low <= settings.batch.delay_seconds <= delay_high:
        issues.append(
            OptionIssue(PAGE_GENERATION, "options.batch_delay", "options.err_range", (delay_low, delay_high))
        )
    quick_low, quick_high = QUICK_COUNT_RANGE
    for index, value in enumerate(settings.batch.quick_counts, start=1):
        if not quick_low <= value <= quick_high:
            issues.append(
                OptionIssue(
                    PAGE_GENERATION,
                    "options.quick_count_button",
                    "options.err_range",
                    (quick_low, quick_high),
                    field_args=(index,),
                )
            )
    return issues


def _validate_resolution(settings: AppSettings) -> list[OptionIssue]:
    """비활성 행은 검사하지 않는다 (Req 5.8은 활성화된 행만 대상)."""
    issues: list[OptionIssue] = []
    for row in settings.resolution.customs:
        if not row.enabled:
            continue
        for field_key, value in (("resolution.width", row.width), ("resolution.height", row.height)):
            if is_valid_dimension(value):
                continue
            if not MIN_DIMENSION <= value <= MAX_DIMENSION:
                issues.append(
                    OptionIssue(
                        PAGE_RESOLUTION,
                        field_key,
                        "options.err_range",
                        (MIN_DIMENSION, MAX_DIMENSION),
                    )
                )
            else:
                issues.append(
                    OptionIssue(PAGE_RESOLUTION, field_key, "options.err_step64", (DIMENSION_STEP,))
                )
    return issues
