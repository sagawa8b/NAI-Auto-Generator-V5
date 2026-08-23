"""서비스 → UI 이벤트 — 타입 있는 불변 객체.

서비스는 콜백 on_event(event) 하나로만 바깥과 통신한다.
ui/qt_bridge.py가 이 이벤트들을 Qt 시그널로 변환한다 (Qt는 여기 없음).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationEvent:
    """모든 생성 이벤트의 베이스."""


@dataclass(frozen=True)
class JobStarted(GenerationEvent):
    total: int  # 0 = 무한


@dataclass(frozen=True)
class ImageStarted(GenerationEvent):
    index: int  # 1부터
    seed: int
    prompt: str  # 와일드카드 전개 후 실제 전송 프롬프트


@dataclass(frozen=True)
class ImageCompleted(GenerationEvent):
    index: int
    path: str
    size_bytes: int
    seed: int


@dataclass(frozen=True)
class WaitingNext(GenerationEvent):
    """다음 이미지까지 딜레이 대기 중 (연속 생성)."""

    next_index: int
    wait_seconds: float


@dataclass(frozen=True)
class ImageRetrying(GenerationEvent):
    """일시적 오류로 같은 이미지를 재시도 대기 중."""

    index: int
    reason: str
    wait_seconds: float
    attempt: int


@dataclass(frozen=True)
class JobFinished(GenerationEvent):
    completed: int
    stopped: bool = False  # 사용자 중지로 종료
    error: str | None = None  # 복구 불가 오류로 중단된 경우 메시지
    error_type: str | None = None  # 예외 클래스명 (UI가 i18n 메시지 매핑에 사용)
    credit_observations: tuple = ()  # CreditObservation 튜플 (measure_credit 배치용)
