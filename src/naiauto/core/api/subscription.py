"""/user/subscription 응답 파싱 + 비밀정보 제거.

V5부터 Opus도 "무제한"이 아니라 자동 충전되는 생성 크레딧을 쓴다.
**2026-08-21 실응답으로 필드 확정**: 최상위 `usage` 객체가 그 크레딧이다.

    "usage": {"isNegative": false, "percent": 100, "timeUntilNextPercent": 7888}

- `percent` — 남은 비율(0~100). 웹 UI의 "100% of Opus Generations remaining"
- `timeUntilNextPercent` — 다음 1%가 차기까지 남은 초.
  하루 충전률 = 86400 / 이 값 → 7888초면 10.95%/일로, 웹 UI가 표시한
  "refills at 11% per day"와 일치한다.
- `isNegative` — 크레딧이 마이너스인 상태로 추정 (관찰값은 false뿐)

웹 UI의 "~1730 images" 같은 장수 환산은 응답에 없다. 클라이언트가 티어별
상수로 추정하는 값이므로 여기서는 계산하지 않는다 (소모량 측정 후 별도 작업).

응답에는 image_cache_secret_key 같은 값이 섞여 있으므로, 밖으로 내보낼 때는
반드시 redact()를 통과시킨다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SECONDS_PER_DAY = 86400

REDACTED = "<redacted>"
SECRET_HINTS = ("token", "secret", "password", "auth", "cookie", "encryption")
# 우리가 이미 해석하고 있는 키 — 이 밖의 키는 "새로 관찰된 것"으로 보고한다
KNOWN_KEYS = frozenset(
    {
        "tier",
        "active",
        "expiresAt",
        "perks",
        "paymentProcessor",
        "paymentProcessorData",
        "isPaypal",
        "isGracePeriod",
        "trainingStepsLeft",
        "accountType",
        "usage",  # V5 생성 크레딧 (2026-08-21 확정)
    }
)


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SECRET_HINTS)


def redact(value: Any, key: str = "") -> Any:
    """비밀정보를 지운 복사본. 구조와 숫자는 그대로 두어 스키마 파악에 쓸 수 있다.

    문자열만 지운다 — 비밀값은 항상 문자열이고, contextTokens 같은 숫자까지
    가리면 정작 알고 싶은 스키마가 사라진다.
    """
    if isinstance(value, str) and _is_secret(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


@dataclass(frozen=True)
class OpusUsage:
    """V5 생성 크레딧 잔량. 응답의 `usage` 객체."""

    percent: int
    seconds_to_next_percent: int
    is_negative: bool = False

    @property
    def percent_per_day(self) -> float:
        """하루에 차오르는 비율. 0이면 알 수 없음 (충전 정보 없음)."""
        if self.seconds_to_next_percent <= 0:
            return 0.0
        return SECONDS_PER_DAY / self.seconds_to_next_percent


def parse_usage(data: dict) -> OpusUsage | None:
    """`usage`가 없으면 None (V4.5 시절 응답이거나 스키마가 또 바뀐 경우)."""
    usage = data.get("usage")
    if not isinstance(usage, dict) or "percent" not in usage:
        return None
    try:
        percent = int(usage.get("percent") or 0)
        seconds = int(usage.get("timeUntilNextPercent") or 0)
    except (TypeError, ValueError):
        return None
    return OpusUsage(
        percent=percent,
        seconds_to_next_percent=seconds,
        is_negative=bool(usage.get("isNegative", False)),
    )


def parse_anlas(data: dict) -> dict:
    """{"fixed", "purchased", "total", "opus", "usage"} — 스키마 변화에 관대하게."""
    steps = data.get("trainingStepsLeft") or {}
    fixed = steps.get("fixedTrainingStepsLeft", 0) or 0
    purchased = steps.get("purchasedTrainingSteps", 0) or 0
    opus = bool((data.get("perks") or {}).get("unlimitedMaxPriority", False))
    return {
        "fixed": fixed,
        "purchased": purchased,
        "total": fixed + purchased,
        "opus": opus,
        "usage": parse_usage(data),
    }


def unknown_keys(data: dict) -> list[str]:
    """아직 해석하지 않는 최상위 키 — V5 크레딧 필드를 찾는 단서."""
    return sorted(key for key in data if key not in KNOWN_KEYS)


__all__ = [
    "KNOWN_KEYS",
    "REDACTED",
    "SECONDS_PER_DAY",
    "OpusUsage",
    "parse_anlas",
    "parse_usage",
    "redact",
    "unknown_keys",
]
