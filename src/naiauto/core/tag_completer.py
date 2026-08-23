"""태그 자동완성 엔진.

Danbooru 등 태그 데이터베이스(CSV/JSON)를 로드하여 접두사 매칭으로
후보 태그를 제안한다. core/ 모듈이므로 Qt 의존성이 없다.

CSV는 V4.5 앱과 같은 두 형식을 모두 읽는다:

    1girl[5097077]     ← 앱에 동봉된 danbooru_tags_post_count.csv 형식
    1girl,5097077      ← 일반 CSV (헤더 행 허용)

설정에 경로가 비어 있으면 `bundled_database_path()`의 내장 DB를 쓴다
(`resolve_database_path()` 참조).
"""

from __future__ import annotations

import bisect
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 앱에 동봉하는 기본 태그 DB 파일 이름.
BUNDLED_DATABASE_NAME = "danbooru_tags_post_count.csv"

#: 포함(contains) 매칭까지 시도하는 최소 토큰 길이 (V4.5와 동일).
CONTAINS_MIN_LENGTH = 4

#: 포함 매칭에서 훑어볼 최대 후보 수 — 긴 토큰에서 전체 스캔이 길어지지 않게.
_MAX_CONTAINS_SCAN = 200

_BRACKET_RE = re.compile(r"^(?P<name>.+)\[(?P<count>\d+)\]$")

#: 프롬프트 가중치 접두사 — "1.5::", "-2::", "::" (단일 콜론은 태그의 일부다).
_WEIGHT_PREFIX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)?::")


def strip_weight_prefix(text: str) -> tuple[str, str]:
    """가중치 접두사와 키워드를 분리한다 ("1.5::blue" → ("1.5::", "blue")).

    접두사가 없으면 ("", 원문). 자동완성은 키워드만 검색하고, 삽입할 때 접두사를
    다시 붙여 사용자가 적어 둔 가중치를 잃지 않는다 (V4.5와 같은 동작).
    """
    match = _WEIGHT_PREFIX_RE.match(text)
    if not match:
        return ("", text)
    return (text[: match.end()], text[match.end() :])


def bundled_database_path() -> Path:
    """패키지에 동봉된 태그 DB 경로 (i18n 리소스와 같은 규칙)."""
    return Path(__file__).resolve().parent.parent / "resources" / "tags" / BUNDLED_DATABASE_NAME


def resolve_database_path(configured: str | None) -> Path:
    """설정 값 → 실제로 읽을 경로. 비어 있으면 내장 DB를 쓴다."""
    text = (configured or "").strip()
    return Path(text) if text else bundled_database_path()


@dataclass(frozen=True)
class TagEntry:
    """A single tag with its popularity (post count)."""

    name: str
    post_count: int


def parse_tag_line(line: str) -> TagEntry | None:
    """`태그[개수]` / `태그,개수` 한 줄을 파싱한다. 개수가 없거나 숫자가 아니면 None."""
    line = line.strip()
    if not line:
        return None

    match = _BRACKET_RE.match(line)
    if match:
        name = match.group("name").strip()
        return TagEntry(name=name, post_count=int(match.group("count"))) if name else None

    if "," in line:
        name, _, count = line.partition(",")
        name = name.strip()
        try:
            post_count = int(count.strip())
        except ValueError:
            return None  # 헤더 행("name,post_count") 등
        return TagEntry(name=name, post_count=post_count) if name and post_count >= 0 else None

    return None


class TagCompleter:
    """Fast prefix-matching tag suggestion engine.

    Tags are stored in a sorted list by normalized name for binary search.
    Suggestions are filtered by prefix match and sorted by post_count descending.
    """

    def __init__(self, database_path: Path | None = None) -> None:
        self._database_path = database_path
        self._entries: list[TagEntry] = []
        # (정규화 이름, 엔트리) 정렬 목록 — 접두사 구간을 이분 탐색으로 잘라 쓴다
        self._sorted: list[tuple[str, TagEntry]] = []
        self._enabled = False
        self._warned = False

    def load(self) -> bool:
        """Load tag database. Returns False if disabled (missing/malformed file)."""
        self._entries = []
        self._sorted = []
        self._enabled = False

        # Check if path is configured
        if self._database_path is None or str(self._database_path) == "":
            if not self._warned:
                logger.warning("Tag database path not configured; tag completer disabled")
                self._warned = True
            return False

        path = Path(self._database_path)

        if not path.exists():
            if not self._warned:
                logger.warning("Tag database file not found: %s; tag completer disabled", path)
                self._warned = True
            return False

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Cannot read tag database file: %s; tag completer disabled", e)
            return False

        # Try JSON first, then the line-based tag formats
        entries = self._try_parse_json(raw)
        if entries is None:
            entries = self._try_parse_lines(raw)

        if entries is None:
            logger.warning(
                "Tag database file could not be parsed (malformed CSV/JSON): %s; tag completer disabled",
                path,
            )
            return False

        self._entries = entries
        self._sorted = sorted(((self._normalize(e.name), e) for e in entries), key=lambda pair: pair[0])
        self._enabled = True
        logger.info("Loaded %d tags from %s", len(entries), path)
        return True

    @property
    def is_enabled(self) -> bool:
        """True if the database was loaded successfully."""
        return self._enabled

    @property
    def tag_count(self) -> int:
        """Number of loaded tag entries; 0 when disabled or not loaded yet."""
        return len(self._entries)

    def suggest(self, prefix: str, limit: int = 20) -> list[TagEntry]:
        """Return up to `limit` tags matching prefix (case-insensitive, underscore-normalized).

        접두사 매칭이 먼저 오고, 토큰이 `CONTAINS_MIN_LENGTH` 이상이면 V4.5처럼
        포함 매칭으로 자리를 채운다. 각 묶음은 post_count 내림차순이다.
        Returns [] if disabled or prefix < 2 chars.
        """
        if not self._enabled:
            return []

        if len(prefix) < 2:
            return []

        # Enforce maximum limit of 20
        limit = min(limit, 20)

        needle = self._normalize(prefix)

        start = bisect.bisect_left(self._sorted, (needle,))
        matched: list[TagEntry] = []
        for name, entry in self._sorted[start:]:
            if not name.startswith(needle):
                break
            matched.append(entry)
        matched.sort(key=lambda e: e.post_count, reverse=True)
        results = matched[:limit]

        if len(results) < limit and len(needle) >= CONTAINS_MIN_LENGTH:
            results.extend(self._contains_matches(needle, limit - len(results), set(results)))
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _contains_matches(self, needle: str, limit: int, taken: set[TagEntry]) -> list[TagEntry]:
        """접두사로는 안 걸리지만 토큰을 포함하는 태그 (V4.5의 두 번째 묶음)."""
        found: list[TagEntry] = []
        for name, entry in self._sorted:
            if entry in taken or name.startswith(needle):
                continue  # 접두사 묶음에서 이미 처리했다
            if needle in name:
                found.append(entry)
                if len(found) >= _MAX_CONTAINS_SCAN:
                    break
        found.sort(key=lambda e: e.post_count, reverse=True)
        return found[:limit]

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize for comparison: lowercase + underscores → spaces."""
        return text.lower().replace("_", " ")

    def _try_parse_json(self, raw: str) -> list[TagEntry] | None:
        """Try parsing as JSON array of {name, post_count} objects."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(data, list):
            return None

        entries: list[TagEntry] = []
        for item in data:
            if not isinstance(item, dict):
                return None
            try:
                name = str(item["name"])
                post_count = int(item["post_count"])
                if name:
                    entries.append(TagEntry(name=name, post_count=post_count))
            except (KeyError, TypeError, ValueError):
                return None

        return entries if entries else None

    def _try_parse_lines(self, raw: str) -> list[TagEntry] | None:
        """`태그[개수]` / `태그,개수` 줄들을 읽는다. 한 줄도 못 읽으면 None."""
        entries = [entry for entry in map(parse_tag_line, raw.splitlines()) if entry is not None]
        return entries or None


__all__ = [
    "BUNDLED_DATABASE_NAME",
    "CONTAINS_MIN_LENGTH",
    "TagCompleter",
    "TagEntry",
    "bundled_database_path",
    "parse_tag_line",
    "resolve_database_path",
    "strip_weight_prefix",
]
