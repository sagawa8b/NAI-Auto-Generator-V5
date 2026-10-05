"""등록 와일드카드의 **줄 내용** 자동완성 — 고정 태그 묶음을 일반 태그처럼 고른다.

`catalog.py`가 와일드카드 **이름**(`__hair__`)을 완성한다면, 여기서는 사용자가 옵션에 등록한
몇몇 와일드카드 파일의 **줄**을 태그 후보에 섞는다. 캐릭터 한 명분 태그(`kaname madoka,
pink hair, ...`)를 한 줄씩 적어 둔 파일을 등록해 두면, 이름 일부만 쳐도 그 줄이 통째로 뜬다.
랜덤 전개가 아니라 **고른 줄 그대로** 들어간다는 점이 `__이름__`과 다르다.

검색 규칙:

1. 줄 전체가 검색어로 시작 → 2. 쉼표로 나눈 조각 하나가 검색어로 시작 →
3. (검색어가 `CONTAINS_MIN_LENGTH` 이상이면) 줄 어딘가에 검색어가 들어 있음.

같은 묶음 안에서는 **등록 순서 → 파일 안 줄 순서**를 지킨다 (사용자가 정한 순서가 곧 우선순위다).
줄을 거르는 규칙(빈 줄·`#` 줄 제외)은 `applier.py`와 같다.

core/ 모듈이므로 Qt 의존성이 없다.
"""

from __future__ import annotations

import bisect
import logging
import os
import time
from dataclasses import dataclass

from ..tag_completer import CONTAINS_MIN_LENGTH
from .catalog import MAX_SUGGESTIONS

logger = logging.getLogger(__name__)

#: 파일 하나에서 읽을 최대 줄 수 — 수만 줄짜리 와일드카드를 등록해도 입력이 굼뜨지 않게.
MAX_LINES_PER_SOURCE = 20000

#: 등록 파일이 바뀌었는지(수정 시각) 다시 확인하기까지의 최소 간격(초). 이름 카탈로그(2초)보다
#: 길게 둔다 — 줄 내용을 다시 읽는 쪽이라 더 무겁고, 파일을 고친 뒤 몇 초 늦게 반영돼도 충분하다.
DEFAULT_RESCAN_INTERVAL = 5.0

#: 검색을 시작하는 최소 글자 수 (태그 자동완성과 같다).
MIN_KEYWORD_LENGTH = 2


@dataclass(frozen=True)
class WildcardLine:
    """후보 한 줄 — 넣을 값과 그 줄이 나온 와일드카드 이름."""

    text: str
    source: str


def wildcard_file_path(directory: str, name: str) -> str:
    """와일드카드 이름(`하위폴더/이름`) → 파일 경로 (`catalog.scan_wildcard_names()`의 역)."""
    return os.path.join(directory, *name.split("/")) + ".txt"


def read_wildcard_lines(path: str) -> list[str]:
    """와일드카드 파일의 유효한 줄 (빈 줄·`#` 줄 제외, 앞뒤 공백 제거). 못 읽으면 빈 목록."""
    try:
        with open(path, encoding="utf8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read wildcard file %s: %s", path, e)
        return []
    lines = [ln.strip() for ln in raw if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) > MAX_LINES_PER_SOURCE:
        logger.warning("Wildcard %s has %d lines; using the first %d", path, len(lines), MAX_LINES_PER_SOURCE)
        lines = lines[:MAX_LINES_PER_SOURCE]
    return lines


def _normalize(text: str) -> str:
    """비교용 정규화 — 태그 자동완성과 같이 대소문자·언더스코어를 무시한다."""
    return text.lower().replace("_", " ").strip()


class WildcardLineSource:
    """등록한 와일드카드 파일들의 줄 색인. 파일이 바뀌면(수정 시각) 알아서 다시 읽는다."""

    def __init__(
        self,
        directory: str,
        names: list[str] | tuple[str, ...] = (),
        *,
        rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
        clock=time.monotonic,
    ) -> None:
        self._directory = str(directory or "")
        self._names: list[str] = _unique(names)
        self._rescan_interval = rescan_interval
        self._clock = clock
        self._checked_at: float | None = None
        #: 마지막으로 읽은 각 파일의 수정 시각 (없으면 None) — 바뀌었는지 비교한다.
        self._mtimes: tuple[float | None, ...] = ()
        self._lines: list[WildcardLine] = []
        #: (정규화 키, 줄 번호, 키 종류) 정렬 목록. 키 종류 0 = 줄 전체, 1 = 쉼표 조각.
        self._index: list[tuple[str, int, int]] = []
        self._normalized: list[str] = []

    # ── 조회 ─────────────────────────────────────────────

    @property
    def directory(self) -> str:
        return self._directory

    @property
    def names(self) -> list[str]:
        """등록된 와일드카드 이름 (등록 순서). 색 배정도 이 순서를 따른다."""
        return list(self._names)

    def line_count(self) -> int:
        self._ensure_fresh()
        return len(self._lines)

    def missing(self) -> list[str]:
        """등록됐지만 폴더에 없는 이름들 (옵션 화면 안내용)."""
        self._ensure_fresh()
        return [name for name, mtime in zip(self._names, self._mtimes, strict=True) if mtime is None]

    def suggest(self, keyword: str, limit: int = MAX_SUGGESTIONS) -> list[WildcardLine]:
        """검색어에 맞는 줄 — 줄 접두사 → 조각 접두사 → 포함 순, 같은 줄은 한 번만."""
        needle = _normalize(keyword)
        if len(needle) < MIN_KEYWORD_LENGTH or not self._names:
            return []
        self._ensure_fresh()
        limit = min(limit, MAX_SUGGESTIONS)

        whole: list[int] = []
        piece: list[int] = []
        start = bisect.bisect_left(self._index, (needle,))
        for key, line_no, kind in self._index[start:]:
            if not key.startswith(needle):
                break
            (whole if kind == 0 else piece).append(line_no)

        chosen: list[int] = []
        seen: set[int] = set()
        for group in (sorted(whole), sorted(piece)):
            for line_no in group:
                if line_no not in seen:
                    seen.add(line_no)
                    chosen.append(line_no)

        if len(chosen) < limit and len(needle) >= CONTAINS_MIN_LENGTH:
            for line_no, text in enumerate(self._normalized):
                if len(chosen) >= limit:
                    break
                if line_no not in seen and needle in text:
                    seen.add(line_no)
                    chosen.append(line_no)

        return [self._lines[line_no] for line_no in chosen[:limit]]

    # ── 갱신 ─────────────────────────────────────────────

    def reload(self) -> None:
        """등록한 파일을 지금 바로 다시 읽는다."""
        self._mtimes = self._current_mtimes()
        self._checked_at = self._clock()

        lines: list[WildcardLine] = []
        seen: set[str] = set()
        for name, mtime in zip(self._names, self._mtimes, strict=True):
            if mtime is None:
                continue
            for text in read_wildcard_lines(wildcard_file_path(self._directory, name)):
                if text in seen:
                    continue  # 여러 파일에 같은 줄이 있으면 먼저 등록한 쪽만
                seen.add(text)
                lines.append(WildcardLine(text=text, source=name))

        index: list[tuple[str, int, int]] = []
        normalized: list[str] = []
        for line_no, line in enumerate(lines):
            full = _normalize(line.text)
            normalized.append(full)
            index.append((full, line_no, 0))
            pieces = {_normalize(p) for p in line.text.split(",")}
            pieces.discard("")
            pieces.discard(full)
            index.extend((p, line_no, 1) for p in pieces)
        index.sort()

        self._lines, self._index, self._normalized = lines, index, normalized
        logger.debug("Loaded %d wildcard lines from %d sources", len(lines), len(self._names))

    def _current_mtimes(self) -> tuple[float | None, ...]:
        mtimes: list[float | None] = []
        for name in self._names:
            try:
                mtimes.append(os.path.getmtime(wildcard_file_path(self._directory, name)))
            except OSError:
                mtimes.append(None)
        return tuple(mtimes)

    def _ensure_fresh(self) -> None:
        if self._checked_at is None:
            self.reload()
            return
        if self._clock() - self._checked_at < self._rescan_interval:
            return
        self._checked_at = self._clock()
        if self._current_mtimes() != self._mtimes:
            self.reload()


def _unique(names) -> list[str]:
    """빈 이름을 빼고 순서를 지키며 중복을 없앤다."""
    result: list[str] = []
    for name in names:
        name = str(name).strip().replace("\\", "/")
        if name and name not in result:
            result.append(name)
    return result


__all__ = [
    "DEFAULT_RESCAN_INTERVAL",
    "MAX_LINES_PER_SOURCE",
    "MIN_KEYWORD_LENGTH",
    "WildcardLine",
    "WildcardLineSource",
    "read_wildcard_lines",
    "wildcard_file_path",
]
