"""서비스 콜백 → Qt 시그널 브리지.

GenerationService는 워커 스레드에서 on_event 콜백을 부른다.
이 QObject의 시그널로 변환하면 Qt가 자동으로 큐드 커넥션 처리해서
슬롯은 항상 메인(GUI) 스레드에서 실행된다.
프로젝트에서 Qt 스레딩을 아는 곳은 이 파일 하나다.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..services.events import GenerationEvent


class QtEventBridge(QObject):
    event_received = Signal(object)

    def __call__(self, event: GenerationEvent) -> None:
        # 워커 스레드에서 호출됨 — emit은 스레드 안전 (queued connection)
        self.event_received.emit(event)


class TagKnowledgeBridge(QObject):
    """`TagKnowledgeService.request()`의 결과 콜백 → 메인 스레드 시그널 (모델, 입력, 후보)."""

    result_ready = Signal(str, str, object)

    def __call__(self, model: str, keyword: str, tags: list) -> None:
        # 작업 스레드에서 호출됨. 위젯이 이미 지워졌으면 RuntimeError — 서비스가 삼킨다.
        self.result_ready.emit(model, keyword, tags)
