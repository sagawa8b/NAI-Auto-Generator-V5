"""단일 진입점 클라이언트.

generate() 흐름: 검증 → ModelSpec의 build_payload → POST → decode_response.
모델별 지식은 전부 ModelSpec에 있으므로 이 파일은 V5가 나와도 바뀌지 않는다.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime
from pathlib import Path

from ..validation import validate_request
from .errors import AuthError, RequestValidationError, ResponseDecodeError
from .model_specs import BASE_URL, ModelSpec, get_spec
from .models import GenerationRequest, GenerationResult
from .session import NAISession
from .subscription import parse_anlas, redact, unknown_keys
from .transport import get_json, post_json

logger = logging.getLogger(__name__)

SUBSCRIPTION_URL = f"{BASE_URL}/user/subscription"


def _normalize_single_character(req: GenerationRequest) -> GenerationRequest:
    """캐릭터가 1명이면 좌표를 0.5, 0.5로 강제 (NAI 규칙)."""
    if len(req.characters) == 1:
        c = req.characters[0]
        if c.center_x != 0.5 or c.center_y != 0.5:
            fixed = dataclasses.replace(c, center_x=0.5, center_y=0.5)
            return dataclasses.replace(req, characters=(fixed,))
    return req


class NAIClient:
    def __init__(
        self,
        session: NAISession,
        debug_headers: bool = False,
        forensics_dir: Path | None = None,
    ) -> None:
        self.session = session
        self.debug_headers = debug_headers
        self.forensics_dir = forensics_dir

    def generate(self, req: GenerationRequest) -> GenerationResult:
        req = _normalize_single_character(req)
        errors = validate_request(req)
        if errors:
            raise RequestValidationError(errors)

        spec = get_spec(req.model)
        if req.sampler not in spec.samplers:
            raise RequestValidationError([f"sampler: {req.sampler!r} not in {list(spec.samplers)}"])
        if req.scheduler not in spec.schedulers:
            raise RequestValidationError([f"scheduler: {req.scheduler!r} not in {list(spec.schedulers)}"])

        payload = spec.build_payload(req, spec)
        binary_parts = spec.build_binary_parts(req, spec) if spec.build_binary_parts else None
        body, content_type = self._post_with_reauth(spec, payload, binary_parts)
        try:
            return spec.decode_response(body, content_type)
        except ResponseDecodeError as e:
            dump = self._dump_forensics(body)
            raise ResponseDecodeError(str(e), dump_path=dump) from e

    def get_subscription(self) -> dict:
        """/user/subscription 응답 원문.

        V5 생성 크레딧 필드를 아직 확정하지 못했으므로 원문을 그대로 돌려주고,
        해석하지 않는 키는 로그로 알린다 (디버그 로그에는 비밀정보를 지운 전문).
        """
        data = get_json(SUBSCRIPTION_URL, self.session.token, debug_headers=self.debug_headers)
        extra = unknown_keys(data)
        if extra:
            logger.info("subscription response has unhandled keys: %s", ", ".join(extra))
        logger.debug("subscription response: %s", redact(data))
        return data

    def get_anlas(self) -> dict:
        """Anlas 잔액 조회. {"fixed", "purchased", "total", "opus"}.
        응답 스키마 변화에 관대하게 파싱한다 (V5 이후 변경 대비)."""
        return parse_anlas(self.get_subscription())

    def _post_with_reauth(
        self, spec: ModelSpec, payload: dict, binary_parts: dict[str, bytes] | None = None
    ) -> tuple[bytes, str]:
        def _post():
            return post_json(
                spec.endpoint,
                self.session.token,
                payload,
                debug_headers=self.debug_headers,
                request_format=spec.request_format,
                binary_parts=binary_parts,
            )

        try:
            resp = _post()
        except AuthError:
            if not self.session.reauthenticate():
                raise
            logger.info("reauthenticated after 401, retrying request once")
            resp = _post()
        return resp.content, resp.headers.get("Content-Type", "")

    def _dump_forensics(self, body: bytes) -> str | None:
        """디코딩 실패한 응답 원문을 파일로 남긴다 (V5 응답 포맷 변화 분석용)."""
        if self.forensics_dir is None:
            return None
        try:
            self.forensics_dir.mkdir(parents=True, exist_ok=True)
            path = (
                self.forensics_dir
                / f"response_{datetime.now():%Y%m%d_%H%M%S}_{int(time.time() * 1000) % 1000:03d}.bin"
            )
            path.write_bytes(body)
            logger.warning("undecodable response dumped to %s", path)
            return str(path)
        except OSError as e:
            logger.error("failed to dump forensics file: %s", e)
            return None
