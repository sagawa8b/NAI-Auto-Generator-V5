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

core/ 모듈이므로 Qt를 모른다. 요청은 `TagKnowledgeService`가 작업 스레드에서 보내고 결과를
콜백으로 넘긴다 — 메인 스레드로 옮기는 일은 UI 쪽(`ui/qt_bridge.py`)이 한다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

SUGGEST_TAGS_URL = "https://image.novelai.net/ai/generate-image/suggest-tags"

#: `count`의 상한 — 이 값이면 마커가 가장 진하다.
MAX_KNOWLEDGE = 10000

#: 요청 제한 시간(초). 자동완성은 기다려 주지 않으므로 짧게 둔다.
REQUEST_TIMEOUT = 5.0

#: 실패한 뒤 이 시간(초) 동안은 다시 요청하지 않는다 — 오프라인일 때 키를 칠 때마다 붙잡지 않게.
RETRY_AFTER = 30.0

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


def fetch_tag_knowledge(model: str, keyword: str, timeout: float = REQUEST_TIMEOUT) -> list[TagKnowledge]:
    """서버에 한 번 묻는다. 네트워크·HTTP 오류는 `requests` 예외 그대로 올린다."""
    resp = requests.get(
        SUGGEST_TAGS_URL,
        params={"model": model, "prompt": normalize_tag(keyword)},
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_suggest_tags(resp.json())


#: 결과 콜백 — (모델, 입력, 후보). 작업 스레드에서 불린다.
ResultCallback = Callable[[str, str, list[TagKnowledge]], None]


class TagKnowledgeService:
    """캐시 + 백그라운드 요청.

    `cached()`는 바로 답하고(없으면 None), `request()`는 작업 스레드에 요청을 맡긴 뒤 끝나면
    `on_done`을 부른다. 같은 (모델, 입력)을 이미 묻는 중이거나 실패 직후(`RETRY_AFTER`)면 요청하지
    않는다. 여러 입력란의 드롭다운이 이 객체 하나를 같이 쓴다.
    """

    def __init__(
        self,
        fetch: Callable[[str, str], list[TagKnowledge]] = fetch_tag_knowledge,
        executor: Executor | None = None,
        cache_size: int = CACHE_SIZE,
        retry_after: float = RETRY_AFTER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._executor = executor
        self._owns_executor = executor is None
        self._cache_size = cache_size
        self._retry_after = retry_after
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], list[TagKnowledge]] = OrderedDict()
        self._in_flight: set[tuple[str, str]] = set()
        self._failed_at: float | None = None
        self._lock = threading.Lock()

    def cached(self, model: str, keyword: str) -> list[TagKnowledge] | None:
        key = (model, normalize_tag(keyword))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def request(self, model: str, keyword: str, on_done: ResultCallback) -> bool:
        """요청을 맡겼으면 True. 캐시에 있거나·묻는 중이거나·쉬는 중이면 False."""
        key = (model, normalize_tag(keyword))
        if not model or not key[1]:
            return False
        with self._lock:
            if key in self._cache or key in self._in_flight:
                return False
            if self._failed_at is not None and self._clock() - self._failed_at < self._retry_after:
                return False
            self._in_flight.add(key)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tag-knowledge")
        self._executor.submit(self._run, key, on_done)
        return True

    def _run(self, key: tuple[str, str], on_done: ResultCallback) -> None:
        model, keyword = key
        try:
            tags = self._fetch(model, keyword)
        except Exception as e:  # noqa: BLE001 — 자동완성 보조 기능이라 어떤 실패도 조용히 넘긴다
            logger.info("suggest-tags failed (%s, %r): %s", model, keyword, e)
            with self._lock:
                self._in_flight.discard(key)
                self._failed_at = self._clock()
            return
        with self._lock:
            self._in_flight.discard(key)
            self._failed_at = None
            self._cache[key] = tags
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        try:
            on_done(model, keyword, tags)
        except Exception:  # noqa: BLE001 — 받는 쪽(위젯)이 이미 사라졌을 수 있다
            logger.debug("tag knowledge callback failed", exc_info=True)

    def shutdown(self) -> None:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


__all__ = [
    "CACHE_SIZE",
    "MAX_KNOWLEDGE",
    "REQUEST_TIMEOUT",
    "RETRY_AFTER",
    "SUGGEST_TAGS_URL",
    "ResultCallback",
    "TagKnowledge",
    "TagKnowledgeService",
    "fetch_tag_knowledge",
    "knowledge_level",
    "normalize_tag",
    "parse_suggest_tags",
]
