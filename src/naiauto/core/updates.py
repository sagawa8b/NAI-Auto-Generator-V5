"""새 버전 확인 — GitHub Releases의 최신 태그를 현재 버전과 비교한다.

공개 배포본은 릴리스 zip으로 나가므로 사용자가 저장소를 들여다보지 않으면 새 버전이
나온 줄 모른다. 앱이 대신 확인해 준다.

core 모듈이라 Qt가 없다. 네트워크 실패·이상한 응답은 전부 삼키고 None을 돌려준다 —
업데이트 확인이 앱을 방해하면 안 된다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

#: 공개 배포 저장소. 릴리스는 여기에만 올라간다.
RELEASES_API = "https://api.github.com/repos/sagawa8b/NAI-Auto-Generator-V5/releases/latest"
RELEASES_PAGE = "https://github.com/sagawa8b/NAI-Auto-Generator-V5/releases/latest"

DEFAULT_TIMEOUT = 5.0

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class ReleaseInfo:
    """최신 릴리스 한 건. `url`은 사용자에게 열어 줄 페이지."""

    tag: str
    url: str


def parse_version(text: str) -> tuple[int, int, int] | None:
    """ "v0.2.0" / "0.2.0" → (0, 2, 0). 형식이 다르면 None (비교하지 않는다)."""
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(candidate: str, current: str) -> bool:
    """candidate가 current보다 높은 버전인가. 둘 중 하나라도 못 읽으면 False."""
    new, old = parse_version(candidate), parse_version(current)
    if new is None or old is None:
        return False
    return new > old


def fetch_latest_release(timeout: float = DEFAULT_TIMEOUT) -> ReleaseInfo | None:
    """최신 릴리스 조회. 실패하면 None (호출 측은 조용히 넘어가면 된다)."""
    try:
        response = requests.get(
            RELEASES_API,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:  # 네트워크·JSON·HTTP 무엇이든 확인 실패일 뿐이다
        logger.info("update check failed: %s", e)
        return None

    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    url = data.get("html_url")
    return ReleaseInfo(tag=tag, url=url if isinstance(url, str) and url else RELEASES_PAGE)


def check_for_update(current: str, timeout: float = DEFAULT_TIMEOUT) -> ReleaseInfo | None:
    """현재 버전보다 새 릴리스가 있으면 그 정보를, 없거나 확인 실패면 None."""
    latest = fetch_latest_release(timeout)
    if latest is None or not is_newer(latest.tag, current):
        return None
    return latest


__all__ = [
    "DEFAULT_TIMEOUT",
    "RELEASES_API",
    "RELEASES_PAGE",
    "ReleaseInfo",
    "check_for_update",
    "fetch_latest_release",
    "is_newer",
    "parse_version",
]
