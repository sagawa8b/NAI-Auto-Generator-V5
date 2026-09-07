"""와일드카드 이름 목록 — 자동완성이 쓰는 가벼운 카탈로그.

`applier.py`가 파일 **내용**을 읽어 프롬프트에 전개한다면, 여기서는 **이름만** 모은다.
프롬프트에 `__`나 `##`를 치는 순간 폴더 안에 무엇이 있는지 보여 주기 위한 것이라
줄 내용까지 읽을 이유가 없다 (와일드카드 파일은 수만 줄이 되기도 한다).

core/ 모듈이므로 Qt 의존성이 없다 — 토큰 파싱과 목록 검색은 Qt 없이 테스트한다.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 폴더를 다시 훑기까지의 최소 간격(초). 앱을 켠 채 파일을 넣어도 곧 목록에 뜨되,
#: 한 글자 칠 때마다 디스크를 훑지는 않는다.
DEFAULT_RESCAN_INTERVAL = 2.0

#: 한 번에 보여 줄 최대 후보 수 (태그 자동완성과 같은 상한).
MAX_SUGGESTIONS = 20

#: 여는 기호 → 닫는 기호. `__`는 랜덤, `__=`는 공유 랜덤, `##`는 루프카드 (applier.py 참고).
CLOSERS = {"__=": "__", "__": "__", "##": "##"}

#: 검사 순서 — `__=`를 `__`보다 먼저 봐야 공유 랜덤이 일반 랜덤으로 잘리지 않는다.
_OPENERS = ("__=", "__", "##")


@dataclass(frozen=True)
class WildcardToken:
    """입력 중인 와일드카드 토큰 — 여는 기호와 그 뒤에 친 글자."""

    opener: str
    keyword: str

    @property
    def closer(self) -> str:
        return CLOSERS[self.opener]

    def insertion(self, name: str) -> str:
        """이름 하나를 고른 결과 — `__folder/name__` 처럼 닫는 기호까지 붙인다."""
        return f"{self.opener}{name}{self.closer}"


def parse_wildcard_token(token: str) -> WildcardToken | None:
    """`__` / `__=` / `##`로 시작하는 토큰을 (여는 기호, 검색어)로 나눈다.

    와일드카드 토큰이 아니거나 **이미 닫힌** 토큰(`__hair__`)이면 None이다. 다 쓴
    자리에서 목록을 다시 띄우면, 방금 고른 이름 위에 또 덮어쓰게 된다.
    """
    for opener in _OPENERS:
        if token.startswith(opener):
            keyword = token[len(opener) :]
            if CLOSERS[opener] in keyword:
                return None  # 이미 닫혔다
            return WildcardToken(opener=opener, keyword=keyword)
    return None


def scan_wildcard_names(directory: str) -> list[str]:
    """폴더 아래 `.txt` 파일 이름을 `하위폴더/이름` 형태로 모은다 (확장자 제외).

    구분자와 확장자 규칙은 `WildcardApplier.load_wildcards()`와 같다 — 여기서 보여 준
    이름을 그대로 프롬프트에 넣으면 전개가 되어야 하기 때문이다.
    """
    if not directory or not os.path.isdir(directory):
        return []

    names: list[str] = []
    try:
        for dirpath, _dirnames, filenames in os.walk(directory):
            rel = os.path.relpath(dirpath, directory)
            prefix = "" if rel == os.curdir else rel.replace(os.sep, "/") + "/"
            for filename in filenames:
                if not filename.endswith(".txt"):
                    continue
                names.append(prefix + os.path.splitext(filename)[0])
    except OSError as e:
        logger.warning("Cannot scan wildcards folder %s: %s", directory, e)
        return []

    names.sort(key=str.lower)
    return names


class WildcardCatalog:
    """와일드카드 이름 목록. 마지막으로 훑은 지 오래되면 알아서 다시 훑는다."""

    def __init__(
        self,
        directory: str,
        *,
        rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
        clock=time.monotonic,
    ) -> None:
        self._directory = str(directory or "")
        self._rescan_interval = rescan_interval
        self._clock = clock
        self._names: list[str] = []
        self._scanned_at: float | None = None

    @property
    def directory(self) -> str:
        return self._directory

    def set_directory(self, directory: str) -> None:
        """훑을 폴더를 바꾼다 (다음 조회에서 새로 읽는다)."""
        self._directory = str(directory or "")
        self._scanned_at = None

    def reload(self) -> None:
        """지금 바로 다시 훑는다."""
        self._names = scan_wildcard_names(self._directory)
        self._scanned_at = self._clock()

    def names(self) -> list[str]:
        self._ensure_fresh()
        return list(self._names)

    def suggest(self, keyword: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
        """검색어로 시작하는 이름을 먼저, 그다음 포함하는 이름을 채워 돌려준다.

        검색어가 비어 있으면(방금 `__`만 친 상태) 폴더 안 목록을 그대로 보여 준다 —
        무엇이 있는지 모르는 채로 이름을 떠올릴 필요가 없게.
        """
        self._ensure_fresh()
        limit = min(limit, MAX_SUGGESTIONS)
        needle = _normalize(keyword)
        if not needle:
            return self._names[:limit]

        starts = [name for name in self._names if _normalize(name).startswith(needle)]
        if len(starts) >= limit:
            return starts[:limit]
        chosen = set(starts)
        contains = [name for name in self._names if name not in chosen and needle in _normalize(name)]
        return (starts + contains)[:limit]

    def _ensure_fresh(self) -> None:
        if self._scanned_at is None or self._clock() - self._scanned_at >= self._rescan_interval:
            self.reload()


def _normalize(text: str) -> str:
    """비교용 정규화 — 대소문자와 경로 구분자를 무시한다 (전개기도 소문자로 찾는다)."""
    return text.lower().replace("\\", "/")


__all__ = [
    "CLOSERS",
    "DEFAULT_RESCAN_INTERVAL",
    "MAX_SUGGESTIONS",
    "WildcardCatalog",
    "WildcardToken",
    "parse_wildcard_token",
    "scan_wildcard_names",
]
