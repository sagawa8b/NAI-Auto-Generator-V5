"""태그 지식 지표 (Tag Knowledge Indicators) — 모델이 태그를 얼마나 아는지.

NovelAI 웹 UI는 태그 자동완성 후보 오른쪽에 원 마커를 그리고, 그 진하기로 **지금 고른 모델이
그 태그를 얼마나 아는지** 보여 준다 (https://docs.novelai.net/en/image/tags). 값은 웹 UI가 부르는
다음 엔드포인트에서 온다 (공식 문서에 없는 내부 API — 2026-10-28 직접 호출로 확인):

    GET https://image.novelai.net/ai/generate-image/suggest-tags?model=<api_name>&prompt=<입력>
    → {"tags": [{"tag": "mika (blue archive)", "count": 10000, "confidence": 0.0}, ...]}

- 로그인이 필요 없고 Anlas도 들지 않는다. 응답은 `text/plain`이지만 본문은 JSON이다.
- `count` — 모델별 인지도. 10000이 상한이다 (`MAX_KNOWLEDGE`). 같은 태그라도 모델마다 다르고,
  모델이 모르는 태그는 아예 목록에 나오지 않는다.
- `confidence` — 입력 글자와 얼마나 맞는지의 점수. **0.0이면 입력으로 시작하는 태그**이고, 0이
  아니면 비슷하기만 한 태그(`mika (track)` → `mikau`)라 후보로 쓰지 않는다 (`is_prefix_match`).
- 언더스코어는 공백으로 바꿔 보내야 한다 (`blue_ey`로는 `blue eyes`가 안 나온다).
- 없는 모델 이름이면 400 `Model not found`.

**요청을 아껴 보낸다.** 이 호스트는 이미지 생성과 같은 `image.novelai.net`이라, 여기서 Cloudflare
확인(403 "Just a moment...")이나 429를 부르면 생성·로그인까지 함께 막힌다 (2026-11-07 실제로 약 5분간
막힌 적이 있다). 그래서 차단 응답을 받으면 `BLOCKED_PAUSE` 동안 아예 묻지 않고, 그 밖의 실패는
간격을 두 배씩 늘리며(`RETRY_AFTER` → `MAX_RETRY_AFTER`), 평소에도 분당 `MAX_REQUESTS_PER_MINUTE`
번을 넘기지 않는다. 연결은 `requests.Session` 하나로 재사용한다.

core/ 모듈이므로 Qt를 모른다. 요청은 `TagKnowledgeService`가 작업 스레드에서 보내고 결과를
콜백으로 넘긴다 — 메인 스레드로 옮기는 일은 UI 쪽(`ui/qt_bridge.py`)이 한다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

SUGGEST_TAGS_URL = "https://image.novelai.net/ai/generate-image/suggest-tags"

#: `count`의 상한 — 이 값이면 마커가 가장 진하다.
MAX_KNOWLEDGE = 10000

#: 요청 제한 시간(초). 자동완성은 기다려 주지 않으므로 짧게 둔다.
REQUEST_TIMEOUT = 5.0

#: 실패한 뒤 이 시간(초) 동안은 다시 요청하지 않는다 — 오프라인일 때 키를 칠 때마다 붙잡지 않게.
#: 연달아 실패하면 두 배씩 늘려 `MAX_RETRY_AFTER`까지.
RETRY_AFTER = 30.0
MAX_RETRY_AFTER = 600.0

#: 서버가 막았을 때(403·429) 쉬는 시간(초). 생성과 같은 호스트라 짧게 두면 생성까지 막힌다.
BLOCKED_PAUSE = 900.0

#: 차단으로 보는 HTTP 상태 — Cloudflare 확인 페이지(403)와 레이트 리밋(429).
BLOCKED_STATUSES = frozenset({403, 429})

#: 1분 동안 보낼 수 있는 최대 요청 수. 넘으면 그 입력은 묻지 않는다 (마커만 빠진다).
MAX_REQUESTS_PER_MINUTE = 20

#: 캐시에 남길 (모델, 입력) 조합 수.
CACHE_SIZE = 512


@dataclass(frozen=True)
class TagKnowledge:
    """서버가 돌려준 후보 한 개."""

    tag: str
    count: int
    confidence: float

    @property
    def level(self) -> float:
        """마커 진하기 0.0~1.0."""
        return knowledge_level(self.count)

    @property
    def is_prefix_match(self) -> bool:
        """입력으로 시작하는 태그인지 — 비슷하기만 한 태그는 후보로 쓰지 않는다."""
        return self.confidence == 0.0


def knowledge_level(count: int) -> float:
    """`count` → 0.0~1.0 (웹 UI의 정확한 공식은 알 수 없어 선형으로 근사한다)."""
    return max(0.0, min(1.0, count / MAX_KNOWLEDGE))


def normalize_tag(text: str) -> str:
    """서버로 보낼 입력이자 후보끼리 견줄 때 쓰는 키.

    로컬 DB는 `blue_eyes`, 서버는 `blue eyes`라 언더스코어를 공백으로 맞춘다.
    """
    return text.replace("_", " ").strip().lower()


def parse_suggest_tags(data: object) -> list[TagKnowledge]:
    """응답 JSON → 후보 목록. 모양이 다른 항목은 건너뛴다."""
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return []
    result: list[TagKnowledge] = []
    for item in tags:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag")
        count = item.get("count")
        confidence = item.get("confidence", 0.0)
        if not isinstance(tag, str) or not tag or isinstance(count, bool):
            continue
        if not isinstance(count, int | float) or not isinstance(confidence, int | float):
            continue
        result.append(TagKnowledge(tag, int(count), float(confidence)))
    return result


class SuggestTagsBlocked(Exception):
    """서버가 요청을 막았다 (403 Cloudflare 확인·429) — 한동안 묻지 말아야 한다."""

    def __init__(self, status: int):
        super().__init__(f"suggest-tags blocked by server (HTTP {status})")
        self.status = status


#: 연결을 재사용하는 HTTP 세션 — 키를 칠 때마다 TLS 연결을 새로 맺지 않게.
_HTTP = requests.Session()


def fetch_tag_knowledge(model: str, keyword: str, timeout: float = REQUEST_TIMEOUT) -> list[TagKnowledge]:
    """서버에 한 번 묻는다.

    403·429는 `SuggestTagsBlocked`로, 그 밖의 네트워크·HTTP 오류는 `requests` 예외 그대로 올린다.
    """
    resp = _HTTP.get(
        SUGGEST_TAGS_URL,
        params={"model": model, "prompt": normalize_tag(keyword)},
        timeout=timeout,
    )
    if resp.status_code in BLOCKED_STATUSES:
        raise SuggestTagsBlocked(resp.status_code)
    resp.raise_for_status()
    return parse_suggest_tags(resp.json())


#: 결과 콜백 — (모델, 입력, 후보). 작업 스레드에서 불린다.
ResultCallback = Callable[[str, str, list[TagKnowledge]], None]


class KnowledgeStore(Protocol):
    """메모리 캐시 뒤에 두는 디스크 캐시 (`core/tag_knowledge_store.py`의 `TagKnowledgeStore`)."""

    def get(self, model: str, keyword: str) -> list[TagKnowledge] | None: ...

    def put(self, model: str, keyword: str, tags: list[TagKnowledge]) -> None: ...

    def close(self) -> None: ...


class TagKnowledgeService:
    """캐시 + 백그라운드 요청.

    `cached()`는 바로 답하고(없으면 None), `request()`는 작업 스레드에 요청을 맡긴 뒤 끝나면
    `on_done`을 부른다. 다음 경우에는 요청하지 않는다:

    - 같은 (모델, 입력)을 이미 묻는 중
    - 실패 뒤 쉬는 중 — 보통 실패는 `RETRY_AFTER`부터 두 배씩, 차단(403·429)은 `BLOCKED_PAUSE`
    - 최근 1분 동안 이미 `MAX_REQUESTS_PER_MINUTE`번 물었음

    `store`를 주면 메모리 캐시에 없을 때 디스크에서 찾고, 서버 응답을 디스크에도 남긴다 — 앱을
    다시 켜도 같은 입력은 서버에 묻지 않는다. 유효 기간은 저장소가 판단한다.

    여러 입력란의 드롭다운이 이 객체 하나를 같이 쓴다.
    """

    def __init__(
        self,
        fetch: Callable[[str, str], list[TagKnowledge]] = fetch_tag_knowledge,
        executor: Executor | None = None,
        cache_size: int = CACHE_SIZE,
        retry_after: float = RETRY_AFTER,
        max_retry_after: float = MAX_RETRY_AFTER,
        blocked_pause: float = BLOCKED_PAUSE,
        max_per_minute: int = MAX_REQUESTS_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        store: KnowledgeStore | None = None,
    ) -> None:
        self._fetch = fetch
        self._store = store
        self._executor = executor
        self._owns_executor = executor is None
        self._cache_size = cache_size
        self._retry_after = retry_after
        self._max_retry_after = max_retry_after
        self._blocked_pause = blocked_pause
        self._max_per_minute = max_per_minute
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], list[TagKnowledge]] = OrderedDict()
        self._in_flight: set[tuple[str, str]] = set()
        #: 이 시각(clock 기준) 전에는 묻지 않는다.
        self._paused_until: float | None = None
        #: 연달아 실패한 횟수 — 쉬는 시간을 두 배씩 늘리는 데 쓴다.
        self._failures = 0
        #: 최근 요청 시각 — 분당 상한을 세는 데 쓴다.
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def pause(self, seconds: float) -> None:
        """밖에서 서버 차단을 알았을 때(예: 생성 요청이 Cloudflare에 막힘) 함께 쉰다."""
        with self._lock:
            until = self._clock() + seconds
            if self._paused_until is None or until > self._paused_until:
                self._paused_until = until

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_until is not None and self._clock() < self._paused_until

    def cached(self, model: str, keyword: str) -> list[TagKnowledge] | None:
        return self._lookup((model, normalize_tag(keyword)))

    def _lookup(self, key: tuple[str, str]) -> list[TagKnowledge] | None:
        """메모리 → 디스크 순으로 찾는다. 디스크에서 찾으면 메모리에 올려 둔다."""
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        if self._store is None:
            return None
        hit = self._store.get(*key)
        if hit is not None:
            with self._lock:
                self._remember(key, hit)
        return hit

    def _remember(self, key: tuple[str, str], tags: list[TagKnowledge]) -> None:
        """메모리 캐시에 넣는다 (잠금을 쥔 채 부른다)."""
        self._cache[key] = tags
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def request(self, model: str, keyword: str, on_done: ResultCallback) -> bool:
        """요청을 맡겼으면 True. 캐시에 있거나·묻는 중이거나·쉬는 중이면 False."""
        key = (model, normalize_tag(keyword))
        if not model or not key[1]:
            return False
        if self._lookup(key) is not None:
            return False
        with self._lock:
            if key in self._cache or key in self._in_flight:
                return False
            now = self._clock()
            if self._paused_until is not None and now < self._paused_until:
                return False
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            if len(self._sent) >= self._max_per_minute:
                return False
            self._sent.append(now)
            self._in_flight.add(key)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tag-knowledge")
        self._executor.submit(self._run, key, on_done)
        return True

    def _run(self, key: tuple[str, str], on_done: ResultCallback) -> None:
        model, keyword = key
        try:
            tags = self._fetch(model, keyword)
        except SuggestTagsBlocked as e:
            logger.warning(
                "suggest-tags blocked (HTTP %s) — pausing tag knowledge for %.0fs",
                e.status,
                self._blocked_pause,
            )
            with self._lock:
                self._in_flight.discard(key)
                self._failures += 1
                self._paused_until = self._clock() + self._blocked_pause
            return
        except Exception as e:  # noqa: BLE001 — 자동완성 보조 기능이라 어떤 실패도 조용히 넘긴다
            with self._lock:
                self._in_flight.discard(key)
                self._failures += 1
                wait = min(self._retry_after * 2 ** (self._failures - 1), self._max_retry_after)
                self._paused_until = self._clock() + wait
            logger.info("suggest-tags failed (%s, %r): %s — retry in %.0fs", model, keyword, e, wait)
            return
        with self._lock:
            self._in_flight.discard(key)
            self._failures = 0
            self._paused_until = None
            self._remember(key, tags)
        store = self._store  # shutdown()이 그사이 None으로 바꿀 수 있다 — 닫힌 저장소의 put은 무시된다
        if store is not None:
            store.put(model, keyword, tags)
        try:
            on_done(model, keyword, tags)
        except Exception:  # noqa: BLE001 — 받는 쪽(위젯)이 이미 사라졌을 수 있다
            logger.debug("tag knowledge callback failed", exc_info=True)

    def shutdown(self) -> None:
        """작업 스레드를 멈추고 디스크 캐시를 닫는다. 늦게 끝난 요청은 디스크에 남지 않는다."""
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._store is not None:
            store, self._store = self._store, None
            store.close()


__all__ = [
    "BLOCKED_PAUSE",
    "BLOCKED_STATUSES",
    "CACHE_SIZE",
    "KnowledgeStore",
    "MAX_KNOWLEDGE",
    "MAX_REQUESTS_PER_MINUTE",
    "MAX_RETRY_AFTER",
    "REQUEST_TIMEOUT",
    "RETRY_AFTER",
    "SUGGEST_TAGS_URL",
    "ResultCallback",
    "SuggestTagsBlocked",
    "TagKnowledge",
    "TagKnowledgeService",
    "fetch_tag_knowledge",
    "knowledge_level",
    "normalize_tag",
    "parse_suggest_tags",
]
