"""NAI API 오류 계층.

혼합 반환값(bytes | (None, str))이나 매직 정수 에러코드 대신
타입 예외로 실패를 전달한다. 서비스 레이어는 예외 타입별로
재시도/중단/재로그인 정책을 결정한다.
"""

from __future__ import annotations


class NAIError(Exception):
    """모든 NAI API 관련 오류의 베이스."""


class RequestValidationError(NAIError):
    """클라이언트 측 파라미터 검증 실패. errors는 위반 항목 목록."""

    def __init__(self, errors: list[str]):
        super().__init__(f"Invalid parameters: {'; '.join(errors)}")
        self.errors = errors


class AuthError(NAIError):
    """401 — 토큰 만료/무효. 세션 재인증 1회 후에도 실패하면 사용자에게 표면화."""


class InsufficientAnlasError(NAIError):
    """402 — Anlas 부족. 루프 중단 대상."""


class RateLimitError(NAIError):
    """429 — 레이트 리밋. retry_after(초)가 있으면 그만큼 대기 후 재시도."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PayloadRejectedError(NAIError):
    """400 등 — 서버가 페이로드를 거부. V5 스펙 추적을 위해 응답 원문을 보존한다."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class ServerBusyError(NAIError):
    """502/503/504/520 — 일시적 서버 오류. 백오프 후 재시도 대상."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status} (server busy)")
        self.status = status


class NetworkError(NAIError):
    """연결 실패/타임아웃. 루프 일시정지 후 재개 대상."""


class ResponseDecodeError(NAIError):
    """응답 본문 해석 실패. V5 포렌식을 위해 원문 덤프 경로를 담는다."""

    def __init__(self, message: str, dump_path: str | None = None):
        super().__init__(message)
        self.dump_path = dump_path


class ModelSpecIncompleteError(NAIError):
    """해당 모델의 ModelSpec이 아직 미완성 (예: V5 캡처 대기 중)."""
