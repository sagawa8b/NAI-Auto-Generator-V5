"""갤러리 검색과 즐겨찾기 (Qt-free).

## 검색

검색어는 **쉼표로 나눈 조건을 모두** 만족해야 걸린다 (AND). 태그에는 공백이 들어가므로
(`blue eyes`) 공백이 아니라 쉼표로 나눈다. 조건 앞에 `-`를 붙이면 그 말이 **없는** 것만
남긴다.

    blue eyes, smile        둘 다 든 이미지
    1girl, -hat             1girl은 있고 hat은 없는 이미지

찾는 곳은 파일 이름과, 메타데이터의 프롬프트·네거티브·캐릭터 프롬프트·시드다.
메타데이터는 파일을 열어야 나오므로 뷰가 백그라운드에서 읽어 채운다 — 아직 읽지 않은
파일은 파일 이름으로만 걸린다.

## 즐겨찾기

경로 목록을 JSON 파일 하나에 둔다. 이미지 파일 자체(메타데이터·이름)는 건드리지 않는다 —
PNG를 다시 쓰면 NovelAI가 넣은 원본 청크 순서가 바뀐다.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from .metadata.reuse import extract_reusable
from .settings.schema import default_data_dir

logger = logging.getLogger(__name__)

FAVORITES_FILE_NAME = "gallery_favorites.json"


def default_favorites_path() -> Path:
    """즐겨찾기 파일의 기본 위치. 테스트는 이 함수를 임시 폴더로 바꿔 끼운다."""
    return default_data_dir() / FAVORITES_FILE_NAME


def searchable_text(metadata: dict | None) -> str:
    """메타데이터에서 검색에 쓸 글자만 뽑아 소문자로 잇는다. 없으면 ""."""
    if not metadata:
        return ""
    settings = extract_reusable(metadata)
    parts: list[str] = [settings.prompt or "", settings.negative_prompt or ""]
    for character in settings.characters or ():
        parts += [character.prompt, character.uc]
    if settings.seed is not None:
        parts.append(str(settings.seed))
    return "\n".join(part for part in parts if part).lower()


def parse_query(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """검색어를 (있어야 하는 말, 없어야 하는 말)로 나눈다. 모두 소문자."""
    include: list[str] = []
    exclude: list[str] = []
    for raw in query.split(","):
        term = raw.strip().lower()
        if not term:
            continue
        if term.startswith("-"):
            if term[1:].strip():
                exclude.append(term[1:].strip())
        else:
            include.append(term)
    return tuple(include), tuple(exclude)


def matches(query: str, filename: str, text: str = "") -> bool:
    """`query`가 이 이미지에 걸리는가. 빈 검색어는 전부 통과."""
    include, exclude = parse_query(query)
    if not include and not exclude:
        return True
    haystack = f"{filename.lower()}\n{text}"
    return all(term in haystack for term in include) and not any(term in haystack for term in exclude)


def _normalize(path: str | Path) -> str:
    """같은 파일을 가리키는 표기를 하나로 (Windows는 대소문자·구분자가 섞여 들어온다)."""
    return os.path.normcase(os.path.abspath(str(path)))


class FavoriteStore:
    """즐겨찾기 경로 집합. 바뀔 때마다 파일에 쓴다.

    파일이 없거나 깨졌으면 빈 목록으로 시작한다 — 즐겨찾기를 못 읽는다고 갤러리가
    안 열리면 안 된다.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_favorites_path()
        self._paths: set[str] = set()
        self.load()

    def load(self) -> None:
        self._paths = set()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        entries = data.get("favorites") if isinstance(data, dict) else None
        if isinstance(entries, list):
            self._paths = {_normalize(entry) for entry in entries if isinstance(entry, str) and entry}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "favorites": sorted(self._paths)}
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("cannot save gallery favorites: %s", e)

    def __len__(self) -> int:
        return len(self._paths)

    def __contains__(self, path: object) -> bool:
        return isinstance(path, (str, Path)) and _normalize(path) in self._paths

    def set_favorite(self, paths: Iterable[str | Path], favorite: bool) -> None:
        """여러 장을 한 번에 켜거나 끈다 (파일은 한 번만 쓴다)."""
        before = set(self._paths)
        for path in paths:
            if favorite:
                self._paths.add(_normalize(path))
            else:
                self._paths.discard(_normalize(path))
        if self._paths != before:
            self.save()

    def toggle(self, path: str | Path) -> bool:
        """한 장을 뒤집는다. 뒤집은 뒤의 상태를 돌려준다."""
        now = path not in self
        self.set_favorite([path], now)
        return now


__all__ = [
    "FAVORITES_FILE_NAME",
    "FavoriteStore",
    "default_favorites_path",
    "matches",
    "parse_query",
    "searchable_text",
]
