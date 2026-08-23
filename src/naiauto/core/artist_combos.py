"""아티스트 콤보 엔진 — 아티스트 태그 그룹의 랜덤/순차 순환.

문법:
    {artist:group_name}       랜덤 선택 (주입된 RNG 사용)
    {artist_loop:group_name}  순차 선택 (사이클마다 인덱스 진행, 마지막 → 0 래핑)

프로토콜 (배치 생성 1 사이클):
    create_index_snapshot() → apply(...) [여러 번 가능, 스냅숏 상태 불변]
    → advance_loopcard_indices()

변경점: stdlib logging 사용, RNG 주입 가능 (테스트 결정성).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Validation constants
_MAX_GROUP_NAME_LEN = 64
_GROUP_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_MAX_TAGS_PER_GROUP = 500
_MAX_TAG_LEN = 200

# Placeholder patterns
_ARTIST_RANDOM_RE = re.compile(r"\{artist:([^}]+)\}")
_ARTIST_LOOP_RE = re.compile(r"\{artist_loop:([^}]+)\}")


@dataclass(frozen=True)
class ArtistComboGroup:
    """A named group of artist tags."""

    name: str
    tags: tuple[str, ...]


class ArtistComboEngine:
    """Loads artist combo groups and replaces placeholders in prompts.

    Follows the same three-phase snapshot protocol as WildcardApplier:
    1. create_index_snapshot() — snapshot sequential indices (cycle start)
    2. apply(text) — replace placeholders using snapshot state (immutable)
    3. advance_loopcard_indices() — advance sequential indices for used groups (cycle end)
    """

    def __init__(self, combos_dir: str, rng: random.Random | None = None) -> None:
        self.combos_dir = combos_dir
        self._rng = rng or random.Random()
        self._groups: dict[str, ArtistComboGroup] = {}
        # Sequential (loop) indices — persisted across cycles
        self._loop_indices: dict[str, int] = {}
        # Snapshot state — frozen at cycle start
        self._snapshot_indices: dict[str, int] = {}
        # Groups used during current apply phase (for advancing)
        self._used_loop_groups: set[str] = set()

    def load(self) -> None:
        """Load all JSON files from combos_dir. Invalid files logged + skipped."""
        self._groups.clear()

        if not os.path.isdir(self.combos_dir):
            logger.warning("Artist combos directory not found: %s", self.combos_dir)
            return

        loaded_count = 0
        for filename in os.listdir(self.combos_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(self.combos_dir, filename)
            if not os.path.isfile(filepath):
                continue

            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Invalid artist combo file '%s': %s", filename, e)
                continue

            if not isinstance(data, dict):
                logger.warning("Invalid artist combo file '%s': expected JSON object at top level", filename)
                continue

            for group_name, tags in data.items():
                if not self._validate_group_name(group_name, filename):
                    continue
                if not self._validate_tags(group_name, tags, filename):
                    continue

                # Filter empty strings and truncate to limits
                valid_tags = tuple(
                    tag
                    for tag in tags[:_MAX_TAGS_PER_GROUP]
                    if isinstance(tag, str) and 1 <= len(tag) <= _MAX_TAG_LEN
                )

                if not valid_tags:
                    logger.warning(
                        "Artist combo group '%s' in '%s' has no valid tags after filtering, treating as non-existent",
                        group_name,
                        filename,
                    )
                    continue

                self._groups[group_name] = ArtistComboGroup(name=group_name, tags=valid_tags)
                loaded_count += 1

        logger.info("Loaded %d artist combo groups from %s", loaded_count, self.combos_dir)

    def create_index_snapshot(self) -> None:
        """Snapshot sequential indices (cycle start)."""
        self._snapshot_indices = self._loop_indices.copy()
        self._used_loop_groups = set()

    def apply(self, text: str) -> str:
        """Replace {artist:name} and {artist_loop:name} placeholders using snapshot state.

        Uses snapshot indices — does not mutate loop state during apply.
        """
        # Apply random artist placeholders
        text = _ARTIST_RANDOM_RE.sub(self._replace_random, text)
        # Apply sequential artist_loop placeholders
        text = _ARTIST_LOOP_RE.sub(self._replace_loop, text)
        return text

    def advance_loopcard_indices(self) -> None:
        """Advance sequential indices for used groups (cycle end)."""
        for group_name in self._used_loop_groups:
            if group_name in self._groups:
                group = self._groups[group_name]
                current = self._loop_indices.get(group_name, 0)
                self._loop_indices[group_name] = (current + 1) % len(group.tags)
        self._used_loop_groups.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _replace_random(self, match: re.Match) -> str:
        """Replace a single {artist:name} match with a random tag from the group."""
        group_name = match.group(1)
        group = self._groups.get(group_name)
        if group is None:
            logger.warning("Artist combo group '%s' not found, leaving placeholder unchanged", group_name)
            return match.group(0)
        return group.tags[self._rng.randrange(len(group.tags))]

    def _replace_loop(self, match: re.Match) -> str:
        """Replace a single {artist_loop:name} match with the tag at the snapshotted index."""
        group_name = match.group(1)
        group = self._groups.get(group_name)
        if group is None:
            logger.warning(
                "Artist combo group '%s' not found for loop, leaving placeholder unchanged", group_name
            )
            return match.group(0)
        idx = self._snapshot_indices.get(group_name, 0)
        # Ensure idx is within bounds (e.g. if group was reloaded with fewer tags)
        idx = idx % len(group.tags)
        self._used_loop_groups.add(group_name)
        return group.tags[idx]

    @staticmethod
    def _validate_group_name(name: str, filename: str) -> bool:
        """Validate group name: 1–64 chars, [a-zA-Z0-9_]."""
        if not isinstance(name, str) or not _GROUP_NAME_RE.match(name):
            logger.warning(
                "Invalid group name '%s' in '%s': must be 1–64 alphanumeric/underscore characters",
                name,
                filename,
            )
            return False
        return True

    @staticmethod
    def _validate_tags(group_name: str, tags: object, filename: str) -> bool:
        """Validate tag array: must be a list of 1–500 strings, each 1–200 chars."""
        if not isinstance(tags, list):
            logger.warning(
                "Invalid tags for group '%s' in '%s': expected array, got %s",
                group_name,
                filename,
                type(tags).__name__,
            )
            return False
        if len(tags) == 0:
            logger.warning(
                "Artist combo group '%s' in '%s' has empty tag array, treating as non-existent",
                group_name,
                filename,
            )
            return False
        if len(tags) > _MAX_TAGS_PER_GROUP:
            logger.warning(
                "Group '%s' in '%s' has %d tags, truncating to %d",
                group_name,
                filename,
                len(tags),
                _MAX_TAGS_PER_GROUP,
            )
        return True
