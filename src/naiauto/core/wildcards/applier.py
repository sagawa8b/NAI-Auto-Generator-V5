"""와일드카드 엔진 — 구 wildcard_applier.py를 동작 동일하게 이식.

문법:
    __folder/file__      랜덤 선택
    __=folder/file__     공유 랜덤 (같은 생성 사이클 안에서 동일 값 반환)
    ##folder/file##      루프카드 (순차 선택)
    ##name*3##           루프카드 반복 (3회 반복 후 다음 항목으로)

프로토콜 (배치 생성 1 사이클):
    create_index_snapshot() → apply_wildcards_with_snapshot(...) [여러 번 가능]
    → advance_loopcard_indices()

변경점: stdlib logging 사용, RNG 주입 가능 (테스트 결정성).
"""

from __future__ import annotations

import logging
import os
import random

logger = logging.getLogger(__name__)

MAX_TRY_AMOUNT = 10


class WildcardApplier:
    def __init__(self, src_wildcards_folder: str, rng: random.Random | None = None):
        self.src_wildcards_folder = src_wildcards_folder
        self._rng = rng or random.Random()
        self._wildcards_dict: dict[str, list[str]] = {}
        self._loopcard_indices: dict[str, int] = {}
        self._repeat_counters: dict[str, dict[str, int]] = {}
        self._current_snapshot: dict[str, int] = {}
        self._used_keys: set[str] = set()
        # 공유 랜덤 캐시 — 같은 생성 사이클 내에서 동일 값 반환
        self._shared_random_cache: dict[str, str] = {}

    def set_src(self, src: str) -> None:
        if os.name == "nt":
            src = os.path.normpath(src)
        self.src_wildcards_folder = src

    def load_wildcards(self) -> None:
        self._wildcards_dict.clear()

        if not os.path.exists(self.src_wildcards_folder):
            logger.warning("Wildcards folder not found: %s", self.src_wildcards_folder)
            return

        wildcard_count = 0
        try:
            for dirpath, _dname_list, fname_list in os.walk(self.src_wildcards_folder):
                path = dirpath.replace(self.src_wildcards_folder, "")
                path = path.replace("\\", "/") + "/"
                path = path[1:] if path.startswith("/") else path

                for filename in fname_list:
                    if not filename.endswith(".txt"):
                        continue
                    src = os.path.join(dirpath, filename)
                    try:
                        with open(src, encoding="utf8") as f:
                            lines = f.readlines()
                    except OSError as e:
                        logger.error("Error loading wildcard file %s: %s", src, e)
                        continue
                    if not lines:
                        continue
                    onlyname = os.path.splitext(os.path.basename(filename))[0]
                    key = path + onlyname
                    valid_lines = [
                        ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")
                    ]
                    if valid_lines:
                        self._wildcards_dict[key.lower()] = valid_lines
                        wildcard_count += 1

            logger.info("Loaded %d wildcards from %s", wildcard_count, self.src_wildcards_folder)
        except OSError as e:
            logger.error("Error loading wildcards: %s", e)

    def create_index_snapshot(self) -> None:
        """루프카드 인덱스 스냅숏 생성 + 공유 랜덤 캐시 초기화 (사이클 시작)."""
        self._current_snapshot = self._loopcard_indices.copy()
        self._used_keys = set()
        self._shared_random_cache = {}

    def apply_wildcards_with_snapshot(self, target_str: str) -> str:
        """스냅숏된 인덱스로 와일드카드 적용 (인덱스 진행 없음)."""
        self.load_wildcards()
        result = target_str

        for apply_once in (
            self._apply_loopcard_once_with_snapshot,
            self._apply_shared_wildcard_once,
            self._apply_wildcard_once,
        ):
            index = 0
            except_list: list[str] = []
            while True:
                result, applied_list = apply_once(result, except_list)
                except_list.extend(applied_list)
                if len(applied_list) == 0 or index > MAX_TRY_AMOUNT:
                    break
                index += 1

        return result

    def advance_loopcard_indices(self) -> None:
        """사용된 루프카드 인덱스만 다음으로 진행 (반복 카운터 고려, 사이클 종료)."""
        for key_data in list(self._used_keys):
            key = key_data
            repeat_count = 1
            if "*" in key_data:
                parts = key_data.split("*")
                if len(parts) == 2:
                    key = parts[0]
                    try:
                        repeat_count = int(parts[1])
                    except ValueError:
                        repeat_count = 1

            if key in self._wildcards_dict:
                wc_list = self._wildcards_dict[key]
                if wc_list:
                    if key not in self._loopcard_indices:
                        self._loopcard_indices[key] = 0

                    counter_key = f"{key}*{repeat_count}"
                    if counter_key not in self._repeat_counters:
                        self._repeat_counters[counter_key] = {"current": 0, "target": repeat_count}
                    self._repeat_counters[counter_key]["current"] += 1

                    if self._repeat_counters[counter_key]["current"] >= repeat_count:
                        self._loopcard_indices[key] = (self._loopcard_indices[key] + 1) % len(wc_list)
                        self._repeat_counters[counter_key]["current"] = 0
                        logger.debug(
                            "Advanced '%s' to index %d after %d repeats",
                            key,
                            self._loopcard_indices[key],
                            repeat_count,
                        )

        self._used_keys.clear()

    def _apply_shared_wildcard_once(
        self, target_str: str, except_list: list[str] | None = None
    ) -> tuple[str, list[str]]:
        """공유 랜덤 와일드카드 (__=name__): 같은 사이클 내 동일 값 반환."""
        if except_list is None:
            except_list = []
        result = target_str
        applied: list[str] = []
        prev_point = 0

        while "__=" in result:
            p_left = result.find("__=", prev_point)
            if p_left == -1:
                break
            p_right = result.find("__", p_left + 3)
            if p_right == -1:
                logger.warning("A single __= exists in shared wildcard syntax")
                break

            str_left = result[0:p_left]
            str_center = result[p_left + 3 : p_right].lower()
            str_right = result[p_right + 2 :]

            if str_center in self._wildcards_dict and str_center not in except_list:
                wc_list = self._wildcards_dict[str_center]
                if wc_list:
                    if str_center in self._shared_random_cache:
                        selected = self._shared_random_cache[str_center]
                    else:
                        selected = wc_list[self._rng.randrange(0, len(wc_list))].strip()
                        self._shared_random_cache[str_center] = selected
                    str_center = selected
                    applied.append(str_center)
                else:
                    logger.warning("Shared wildcard '%s' has no entries", str_center)
                    str_center = "__=" + str_center + "__"
            else:
                logger.warning("Unknown shared wildcard '%s'", str_center)
                str_center = "__=" + str_center + "__"

            result_left = str_left + str_center
            prev_point = len(result_left)
            result = result_left + str_right

        return result, applied

    def _apply_wildcard_once(
        self, target_str: str, except_list: list[str] | None = None
    ) -> tuple[str, list[str]]:
        if except_list is None:
            except_list = []
        result = target_str
        applied: list[str] = []
        prev_point = 0

        while "__" in result:
            p_left = result.find("__", prev_point)
            if p_left == -1:
                break
            p_right = result.find("__", p_left + 2)
            if p_right == -1:
                logger.warning("A single __ exists in wildcard syntax")
                break

            str_left = result[0:p_left]
            str_center = result[p_left + 2 : p_right].lower()
            str_right = result[p_right + 2 :]

            if str_center in self._wildcards_dict and str_center not in except_list:
                wc_list = self._wildcards_dict[str_center]
                if wc_list:
                    str_center = wc_list[self._rng.randrange(0, len(wc_list))].strip()
                    applied.append(str_center)
                else:
                    logger.warning("Wildcard '%s' has no entries", str_center)
                    str_center = "__" + str_center + "__"
            else:
                logger.warning("Unknown wildcard '%s'", str_center)
                str_center = "__" + str_center + "__"

            result_left = str_left + str_center
            prev_point = len(result_left)
            result = result_left + str_right

        return result, applied

    def _apply_loopcard_once_with_snapshot(
        self, target_str: str, except_list: list[str] | None = None
    ) -> tuple[str, list[str]]:
        """루프카드 (##name## / ##name*N##): 스냅숏 인덱스로 순차 선택."""
        if except_list is None:
            except_list = []
        result = target_str
        applied: list[str] = []
        prev_point = 0

        while "##" in result:
            p_left = result.find("##", prev_point)
            if p_left == -1:
                break
            p_right = result.find("##", p_left + 2)
            if p_right == -1:
                break

            str_left = result[0:p_left]
            str_center = result[p_left + 2 : p_right].lower().strip()
            str_right = result[p_right + 2 :]

            repeat_count = 1
            wildcard_name = str_center
            original_pattern = str_center

            if "*" in str_center:
                parts = str_center.split("*")
                if len(parts) == 2 and parts[0] and parts[1]:
                    try:
                        repeat_count = int(parts[1])
                        wildcard_name = parts[0]
                        if repeat_count <= 0:
                            raise ValueError("repeat count must be positive")
                    except ValueError:
                        str_center = "##" + str_center + "##"
                        result_left = str_left + str_center
                        prev_point = len(result_left)
                        result = result_left + str_right
                        continue

            if wildcard_name in self._wildcards_dict and wildcard_name not in except_list:
                wc_list = self._wildcards_dict[wildcard_name]
                if wc_list:
                    self._used_keys.add(original_pattern)
                    idx = self._current_snapshot.get(wildcard_name, 0)
                    str_center = wc_list[idx].strip()
                    applied.append(str_center)
                    logger.debug("Applied loopcard '%s' at index %d", original_pattern, idx)
                else:
                    str_center = "##" + str_center + "##"
            else:
                str_center = "##" + str_center + "##"

            result_left = str_left + str_center
            prev_point = len(result_left)
            result = result_left + str_right

        return result, applied

    def apply_wildcards(self, target_str: str) -> str:
        """스냅숏 프로토콜 없이 1회성 적용 (단일 생성 편의용)."""
        self.create_index_snapshot()
        result = self.apply_wildcards_with_snapshot(target_str)
        self.advance_loopcard_indices()
        return result
