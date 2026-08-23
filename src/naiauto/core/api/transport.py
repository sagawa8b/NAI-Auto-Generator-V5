"""HTTP 전송 레이어 — 상태코드를 타입 예외로 변환하고 백오프 재시도를 담당.

debug_headers=True 로 두면 모든 응답 헤더를 DEBUG 로그로 남긴다.
V5 초기에 새 x-ratelimit-* 류 헤더를 추측이 아니라 관찰로 발견하기 위한 장치.
"""

from __future__ import annotations

import json
import logging
import time

import requests

from .errors import (
    AuthError,
    InsufficientAnlasError,
    NetworkError,
    PayloadRejectedError,
    RateLimitError,
    ServerBusyError,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = (502, 503, 504, 520)


def _raise_for_status(resp: requests.Response) -> None:
    status = resp.status_code
    if status == 401:
        raise AuthError("Authentication failed (401). Token invalid or expired.")
    if status == 402:
        raise InsufficientAnlasError("Payment required (402). Insufficient Anlas.")
    if status == 429:
        retry_after: float | None = None
        header = resp.headers.get("Retry-After")
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        raise RateLimitError("Rate limited (429).", retry_after=retry_after)
    if status in RETRYABLE_STATUS:
        raise ServerBusyError(status)
    if 400 <= status < 600:
        raise PayloadRejectedError(status, resp.text)


def _log_headers(resp: requests.Response, debug_headers: bool) -> None:
    if debug_headers:
        logger.debug("response headers: %s", dict(resp.headers))


def post_json(
    url: str,
    token: str,
    payload: dict,
    timeout: float = 180,
    max_retries: int = 3,
    debug_headers: bool = False,
    request_format: str = "json",
    binary_parts: dict[str, bytes] | None = None,
) -> requests.Response:
    """payload POST. 일시적 오류(5xx/네트워크)만 백오프 재시도하고,
    그 외 오류는 즉시 타입 예외로 변환한다.

    request_format:
      - "json": raw JSON 본문 (V4 시대 API)
      - "multipart": multipart/form-data의 "request" JSON 파트 (V5 형식,
        filename="blob") — Content-Type과 boundary는 requests가 생성

    binary_parts: multipart일 때 함께 붙일 {파트 이름: 바이트}. V5는
    parameters.image/mask에 파트 "이름"을 넣고 실제 바이트는 여기로 보낸다.
    """
    headers = {"Authorization": f"Bearer {token}"}
    kwargs: dict = {}
    if request_format == "multipart":
        files = {"request": ("blob", json.dumps(payload), "application/json")}
        for name, data in (binary_parts or {}).items():
            files[name] = (name, data, "image/png")
        kwargs["files"] = files
    else:
        kwargs["json"] = payload
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, timeout=timeout, **kwargs)
            _log_headers(resp, debug_headers)
            _raise_for_status(resp)
            return resp
        except ServerBusyError as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2 * attempt)
                continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2 * attempt)
                continue
            raise NetworkError(f"Network error after {max_retries} attempts: {e}") from e
    raise NetworkError(f"POST {url} failed: {last_error}")


def get_json(
    url: str,
    token: str,
    timeout: float = 10,
    debug_headers: bool = False,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise NetworkError(f"Network error: {e}") from e
    _log_headers(resp, debug_headers)
    _raise_for_status(resp)
    return resp.json()
