"""아레나 기록을 **파일 하나로** 담고 되돌린다 (Qt-free).

아레나 폴더(`arena.json` + `images/`)는 늘 하나뿐이라, 다른 주제로 새로 시작하면
지금까지 쌓은 점수와 그림을 덮어써야 했다. 이 모듈은 그 한 벌을 통째로 zip에 담고
(`export_archive`), 나중에 그대로 되살린다 (`import_archive`).

zip 안:

    arena.json          작가 명단 + 조합 + 점수 (아레나 폴더의 것과 같은 형식)
    manifest.json       무엇이 들어 있는지 (열어 보기 전에 읽는 요약)
    images/<파일명>.png  조합이 가리키는 그림만. 고아 파일은 담지 않는다

**폴더가 아니라 zip인 이유**: 한 덩어리라 옮기기 쉽고, 그림 수백 장이 압축되며,
반쯤 복사된 폴더 같은 어중간한 상태가 생기지 않는다. 압축 없이 폴더로 풀고 싶으면
탐색기에서 zip을 그냥 풀면 된다.

읽는 쪽은 **남이 만든 파일일 수 있다고 보고** 다룬다 — 경로가 폴더 밖을 가리키는
항목(zip slip)은 버리고, 담긴 이미지 이름도 파일명만 남긴다.
"""

from __future__ import annotations

import json
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .models import ArenaState
from .store import DATA_FILENAME, IMAGES_DIRNAME, ArenaStore

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"
ARCHIVE_SUFFIX = ".zip"
ARCHIVE_FORMAT = 1

#: 담을 수 있는 이미지 확장자. 이 목록에 없는 것은 아예 넣지도 꺼내지도 않는다.
_IMAGE_SUFFIXES = (".png", ".webp")


class ArchiveError(Exception):
    """기록 파일을 읽거나 쓸 수 없다 (사용자에게 그대로 보여 줄 메시지)."""


@dataclass(frozen=True)
class ArchiveSummary:
    """열어 보기 전에 알 수 있는 것 — 확인 대화에 그대로 쓴다."""

    artists: int = 0
    combos: int = 0
    matches: int = 0
    images: int = 0
    app_version: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "format": ARCHIVE_FORMAT,
            "artists": self.artists,
            "combos": self.combos,
            "matches": self.matches,
            "images": self.images,
            "app_version": self.app_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> ArchiveSummary:
        """손으로 고쳤을 수 있다 — 이상한 값은 기본값으로 떨어뜨린다."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            artists=_int(data.get("artists")),
            combos=_int(data.get("combos")),
            matches=_int(data.get("matches")),
            images=_int(data.get("images")),
            app_version=str(data.get("app_version") or ""),
            created_at=_float(data.get("created_at")),
        )


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ── 쓰기 ────────────────────────────────────────────────────────────────


def export_archive(
    store: ArenaStore,
    state: ArenaState,
    path: Path,
    *,
    app_version: str = "",
) -> ArchiveSummary:
    """지금 기록과 그림을 zip 하나로 담는다.

    파일이 없는 조합도 그대로 담는다 — 점수와 전적은 그림 없이도 값이 있다.

    Raises:
        ArchiveError: 쓸 수 없을 때.
    """
    images = [
        (combo.image_path, store.images_dir / combo.image_path) for combo in state.combos if combo.image_path
    ]
    present = [(name, source) for name, source in images if source.is_file()]
    summary = ArchiveSummary(
        artists=len(state.artists),
        combos=len(state.combos),
        matches=state.total_matches,
        images=len(present),
        app_version=app_version,
        created_at=time.time(),
    )

    # 임시 이름으로 쓴 뒤 갈아 끼운다 — 도중에 실패해도 멀쩡한 이전 파일이
    # 반쯤 덮인 채로 남지 않는다.
    tmp = path.with_name(f"{path.name}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                MANIFEST_FILENAME,
                json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
            )
            archive.writestr(
                DATA_FILENAME,
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            )
            for name, source in present:
                archive.write(source, f"{IMAGES_DIRNAME}/{name}")
        tmp.replace(path)
    except (OSError, zipfile.BadZipFile) as e:
        tmp.unlink(missing_ok=True)
        raise ArchiveError(str(e)) from e
    return summary


# ── 읽기 ────────────────────────────────────────────────────────────────


def read_summary(path: Path) -> ArchiveSummary:
    """열지 않고 요약만 읽는다 — 확인 대화에서 "무엇을 불러오는지" 보여 주려고.

    `manifest.json`이 없거나 깨졌으면 `arena.json`을 세어 본다 (손으로 만든
    zip이나 옛 파일도 받아 주기 위해서다).

    Raises:
        ArchiveError: zip이 아니거나 `arena.json`이 없을 때.
    """
    with _open(path) as archive:
        names = set(archive.namelist())
        if DATA_FILENAME not in names:
            raise ArchiveError(f"{DATA_FILENAME} not in {path.name}")
        if MANIFEST_FILENAME in names:
            try:
                return ArchiveSummary.from_dict(json.loads(archive.read(MANIFEST_FILENAME)))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("archive manifest unreadable (%s): %s", path, e)
        state = _read_state(archive)
        return ArchiveSummary(
            artists=len(state.artists),
            combos=len(state.combos),
            matches=state.total_matches,
            images=sum(1 for name in names if _image_name(name)),
        )


def import_archive(store: ArenaStore, path: Path) -> ArenaState:
    """기록 파일을 아레나 폴더에 풀고, 읽어 들인 상태를 돌려준다.

    **지금 기록을 덮어쓴다.** 부르기 전에 사용자에게 확인을 받아야 한다.
    그림은 같은 이름이 있으면 덮어쓴다 (같은 조합의 같은 그림이다).

    Raises:
        ArchiveError: 읽을 수 없거나 쓸 수 없을 때.
    """
    with _open(path) as archive:
        state = _read_state(archive)
        store.ensure_dirs()
        try:
            for info in archive.infolist():
                name = _image_name(info.filename)
                if name is None:
                    continue
                with archive.open(info) as source:
                    (store.images_dir / name).write_bytes(source.read())
        except OSError as e:
            raise ArchiveError(str(e)) from e

    # 그림을 먼저 풀고 기록을 나중에 저장한다 — 반대로 하면 중간에 실패했을 때
    # "그림 있음"이라 적힌 조합의 파일이 없다.
    if not store.save(state):
        raise ArchiveError(f"cannot write {store.data_path}")
    store.prune_missing_images(state)
    return state


def _open(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ArchiveError(str(e)) from e


def _read_state(archive: zipfile.ZipFile) -> ArenaState:
    try:
        data = json.loads(archive.read(DATA_FILENAME))
    except (KeyError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ArchiveError(str(e)) from e
    return ArenaState.from_dict(data)


def _image_name(entry: str) -> str | None:
    """zip 항목이 담아도 되는 그림이면 **파일명만** 돌려준다. 아니면 None.

    남이 건넨 파일일 수 있다 — `../`나 절대 경로로 폴더 밖에 쓰게 두면 안 된다
    (zip slip). 경로를 신뢰하는 대신 마지막 조각만 취하고, 확장자로 한 번 더 거른다.
    """
    if entry.endswith("/"):
        return None
    parts = entry.replace("\\", "/").split("/")
    if len(parts) != 2 or parts[0] != IMAGES_DIRNAME:
        return None
    name = parts[1]
    if not name or name in (".", "..") or Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return name


__all__ = [
    "ARCHIVE_SUFFIX",
    "ArchiveError",
    "ArchiveSummary",
    "export_archive",
    "import_archive",
    "read_summary",
]
