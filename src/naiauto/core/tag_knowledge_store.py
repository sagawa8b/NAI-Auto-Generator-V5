"""태그 지식 지표의 디스크 캐시 — 앱을 다시 켜도 같은 입력은 서버에 묻지 않게.

`TagKnowledgeService`의 메모리 캐시(`CACHE_SIZE`)는 앱을 끄면 사라진다. 쓰는 태그는 대개 반복되므로
서버 응답을 SQLite 파일 하나에 남겨 두면, 며칠만 지나도 요청 대부분을 여기서 답할 수 있다.
서버 요청이 줄면 같은 호스트(`image.novelai.net`)로 가는 생성이 Cloudflare에 막힐 일도 준다.

- **사용자가 실제로 친 입력만** 쌓인다 (미리 받아 두지 않는다). 파일은 사용자 데이터 폴더에 있고
  배포물에 들어가지 않는다.
- 키는 (모델 API 이름, 정규화한 입력), 값은 서버가 돌려준 후보 목록과 받은 시각(유닉스 초).
- `TTL`(90일)이 지난 값은 없는 것으로 친다 — 그러면 서비스가 서버에 다시 묻고 덮어쓴다. 모델이
  바뀌어도 그만큼 뒤에는 따라간다. 열 때 지난 줄을 지우고, `MAX_ROWS`를 넘으면 오래된 것부터 지운다.
- 파일이 깨졌거나 쓸 수 없으면 경고만 남기고 디스크 캐시 없이 동작한다 (보조 기능이라 앱을 막지 않는다).

core/ 모듈이므로 Qt를 모른다. 서비스의 작업 스레드와 메인 스레드가 같이 쓰므로 연결 하나를 잠금으로
보호한다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .settings.schema import default_data_dir
from .tag_knowledge import TagKnowledge

logger = logging.getLogger(__name__)

#: 기본 파일 이름 (사용자 데이터 폴더 아래).
STORE_FILE_NAME = "tag_knowledge.sqlite3"

#: 값이 유효한 기간(초) — 90일.
TTL = 90 * 24 * 60 * 60

#: 남길 최대 줄 수. 줄 하나가 수백 바이트라 이만큼이어도 수 MB다.
MAX_ROWS = 50_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    model TEXT NOT NULL,
    keyword TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    tags TEXT NOT NULL,
    PRIMARY KEY (model, keyword)
)
"""


def encode_tags(tags: list[TagKnowledge]) -> str:
    return json.dumps([[t.tag, t.count, t.confidence] for t in tags], ensure_ascii=False)


def decode_tags(text: str) -> list[TagKnowledge] | None:
    """저장된 문자열 → 후보 목록. 모양이 다르면 None (그 줄은 없는 것으로 친다)."""
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    tags: list[TagKnowledge] = []
    for row in rows:
        if not (isinstance(row, list) and len(row) == 3):
            return None
        tag, count, confidence = row
        if not isinstance(tag, str) or isinstance(count, bool) or not isinstance(count, int):
            return None
        if not isinstance(confidence, int | float):
            return None
        tags.append(TagKnowledge(tag, count, float(confidence)))
    return tags


class TagKnowledgeStore:
    """(모델, 입력) → 후보 목록을 SQLite에 남긴다.

    열기에 실패하면 생성자가 `sqlite3.Error`/`OSError`를 올린다 — 부르는 쪽이 저장소 없이 가면 된다.
    연 뒤의 읽기·쓰기 오류는 한 번 경고하고 그 뒤로는 조용히 아무것도 하지 않는다.
    """

    def __init__(
        self,
        path: Path,
        ttl: float = TTL,
        max_rows: int = MAX_ROWS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._ttl = ttl
        self._max_rows = max_rows
        self._clock = clock
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        try:
            self._conn.execute(_SCHEMA)
            self.prune()
        except sqlite3.Error:
            self._conn.close()
            self._conn = None
            raise

    @property
    def path(self) -> Path:
        return self._path

    def _disable(self, error: Exception) -> None:
        logger.warning("tag knowledge store disabled (%s): %s", self._path, error)
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def get(self, model: str, keyword: str) -> list[TagKnowledge] | None:
        """유효 기간 안의 값. 없거나 지났거나 깨졌으면 None."""
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT fetched_at, tags FROM knowledge WHERE model = ? AND keyword = ?",
                    (model, keyword),
                ).fetchone()
            except sqlite3.Error as e:
                self._disable(e)
                return None
        if row is None:
            return None
        fetched_at, text = row
        if self._clock() - fetched_at >= self._ttl:
            return None
        return decode_tags(text)

    def put(self, model: str, keyword: str, tags: list[TagKnowledge]) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO knowledge (model, keyword, fetched_at, tags) VALUES (?, ?, ?, ?)",
                    (model, keyword, self._clock(), encode_tags(tags)),
                )
            except sqlite3.Error as e:
                self._disable(e)

    def prune(self) -> int:
        """지난 줄과 `max_rows`를 넘는 오래된 줄을 지운다. 지운 줄 수를 돌려준다."""
        conn = self._conn
        if conn is None:
            return 0
        removed = conn.execute(
            "DELETE FROM knowledge WHERE fetched_at <= ?", (self._clock() - self._ttl,)
        ).rowcount
        removed += conn.execute(
            "DELETE FROM knowledge WHERE rowid NOT IN "
            "(SELECT rowid FROM knowledge ORDER BY fetched_at DESC LIMIT ?)",
            (self._max_rows,),
        ).rowcount
        if removed:
            logger.debug("tag knowledge store pruned %d rows", removed)
        return removed

    def __len__(self) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            return self._conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def default_store_path() -> Path:
    """사용자 데이터 폴더의 저장소 파일 (테스트는 `tests/conftest.py`가 임시 폴더로 돌린다)."""
    return default_data_dir() / STORE_FILE_NAME


def open_store(path: Path | None = None) -> TagKnowledgeStore | None:
    """저장소를 연다 (경로를 빼면 `default_store_path()`). 실패하면 경고만 남기고 None."""
    if path is None:
        path = default_store_path()
    try:
        return TagKnowledgeStore(path)
    except (sqlite3.Error, OSError) as e:
        logger.warning("cannot open tag knowledge store %s: %s", path, e)
        return None


__all__ = [
    "MAX_ROWS",
    "STORE_FILE_NAME",
    "TTL",
    "TagKnowledgeStore",
    "decode_tags",
    "default_store_path",
    "encode_tags",
    "open_store",
]
