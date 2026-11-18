"""전체 백업 — 설정·프리셋·와일드카드·아티스트 조합 등을 zip 하나로 (Qt-free).

PC를 옮기거나 다시 깔 때 챙길 것이 여러 폴더에 흩어져 있다. 이 모듈은 그것들을 zip
하나로 묶고(`create_backup`), 지금 설정된 폴더로 되푼다(`restore_backup`).

## 담는 것

| 구획 | zip 안의 자리 | 내용 |
|---|---|---|
| `settings` | `settings/settings_v5.json` | 시스템 설정 파일 |
| `presets` | `presets/` | 프리셋 폴더 (세팅 파일·씬 세트 포함) |
| `wildcards` | `wildcards/` | 와일드카드 폴더 |
| `artist_combos` | `artist_combos/` | 아티스트 조합 폴더 |
| `app_data` | `data/` | 씬 큐·갤러리 즐겨찾기·LLM 프롬프트 세트·크레딧 측정값 |
| `arena` | `arena/arena.json` | 그림체 아레나 기록 (그림 파일은 빼고) |

**담지 않는 것**: API 토큰(OS 키링에만 있다), 결과 이미지, 아레나 그림, WD14 모델, 로그.

## 되풀 때

- 폴더는 백업 속 경로가 아니라 **지금 이 PC에 설정된 폴더**로 푼다. 다른 PC에서 만든
  백업의 `D:\\...` 경로를 그대로 믿지 않는다.
- zip 안의 경로가 대상 폴더를 벗어나면(`../`) 그 항목은 건너뛴다.
- 이미 있는 파일은 기본으로 건너뛴다 (`overwrite=True`면 덮어쓴다).
- 설정은 파일로 쓰지 않고 `AppSettings`로 돌려준다 — 앱이 종료할 때 화면 값으로 설정
  파일을 다시 쓰기 때문에, 적용은 호출한 쪽(`MainWindow`)이 맡는다. 이때 폴더 경로는
  지금 PC의 값을 지킨다 (`merge_restored_settings`).
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .settings.schema import AppSettings
from .settings.store import migrate_settings_data

logger = logging.getLogger(__name__)

FORMAT_NAME = "nai-auto-v5-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"

SECTION_SETTINGS = "settings"
SECTION_PRESETS = "presets"
SECTION_WILDCARDS = "wildcards"
SECTION_ARTIST_COMBOS = "artist_combos"
SECTION_APP_DATA = "app_data"
SECTION_ARENA = "arena"
SECTIONS = (
    SECTION_SETTINGS,
    SECTION_PRESETS,
    SECTION_WILDCARDS,
    SECTION_ARTIST_COMBOS,
    SECTION_APP_DATA,
    SECTION_ARENA,
)

_SETTINGS_ARCNAME = "settings/settings_v5.json"
_ARENA_ARCNAME = "arena/arena.json"
_FOLDER_PREFIX = {
    SECTION_PRESETS: "presets/",
    SECTION_WILDCARDS: "wildcards/",
    SECTION_ARTIST_COMBOS: "artist_combos/",
}
_DATA_PREFIX = "data/"

#: 다른 PC의 값을 가져오면 안 되는 설정 항목 — 그 PC의 폴더·파일 경로다.
_MACHINE_PATH_FIELDS = (
    "save_dir",
    "wildcards_dir",
    "presets_dir",
    "artist_combos_dir",
    "arena_dir",
    "wd14_dir",
    "gallery_dir",
    "log_dir",
    "tag_database_path",
)


class BackupError(Exception):
    """백업 파일을 만들거나 읽을 수 없다. `code`는 UI의 번역 키(`backup.err_<code>`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BackupLocations:
    """백업이 읽고 쓰는 자리 — 전부 지금 PC의 경로다."""

    settings_file: Path
    presets_dir: Path
    wildcards_dir: Path
    artist_combos_dir: Path
    #: `data/` 구획에 담을 낱개 파일들 (없는 파일은 건너뛴다). 되풀 때는 이름으로 짝짓는다.
    data_files: tuple[Path, ...]
    arena_file: Path

    def folder(self, section: str) -> Path:
        return {
            SECTION_PRESETS: self.presets_dir,
            SECTION_WILDCARDS: self.wildcards_dir,
            SECTION_ARTIST_COMBOS: self.artist_combos_dir,
        }[section]


@dataclass
class BackupResult:
    """구획별로 몇 개를 담았는지 (또는 되풀었는지)."""

    counts: dict[str, int] = field(default_factory=dict)
    skipped: int = 0  # 되풀 때: 이미 있어서 건너뛴 파일 수
    rejected: int = 0  # 되풀 때: 대상 폴더를 벗어나는 경로라 버린 항목 수
    settings: AppSettings | None = None  # 되풀 때: 백업에 든 설정 (구획을 골랐고 읽혔을 때)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def _add(self, section: str, amount: int = 1) -> None:
        self.counts[section] = self.counts.get(section, 0) + amount


def suggested_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"nai-auto-v5-backup_{stamp}.zip"


def _folder_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*") if path.is_file())


def create_backup(
    destination: Path,
    locations: BackupLocations,
    sections: set[str] | None = None,
    *,
    app_version: str = "",
) -> BackupResult:
    """고른 구획을 zip으로 쓴다. `sections`가 None이면 전부.

    Raises:
        BackupError: zip을 쓸 수 없을 때 (`write_failed`).
    """
    chosen = set(SECTIONS) if sections is None else set(sections)
    result = BackupResult()
    destination = Path(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if SECTION_SETTINGS in chosen and locations.settings_file.is_file():
                archive.write(locations.settings_file, _SETTINGS_ARCNAME)
                result._add(SECTION_SETTINGS)
            for section, prefix in _FOLDER_PREFIX.items():
                if section not in chosen:
                    continue
                base = locations.folder(section)
                for path in _folder_files(base):
                    archive.write(path, prefix + path.relative_to(base).as_posix())
                    result._add(section)
            if SECTION_APP_DATA in chosen:
                for path in locations.data_files:
                    if path.is_file():
                        archive.write(path, _DATA_PREFIX + path.name)
                        result._add(SECTION_APP_DATA)
            if SECTION_ARENA in chosen and locations.arena_file.is_file():
                archive.write(locations.arena_file, _ARENA_ARCNAME)
                result._add(SECTION_ARENA)
            manifest = {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "app_version": app_version,
                "created": datetime.now().isoformat(timespec="seconds"),
                "counts": result.counts,
            }
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    except OSError as e:
        raise BackupError("write_failed", str(e)) from e
    return result


def read_manifest(source: Path) -> dict:
    """백업의 목록표. 이 앱의 백업이 아니면 `BackupError` (`not_a_backup`)."""
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BackupError("not_a_backup", str(e)) from e
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise BackupError("not_a_backup")
    counts = manifest.get("counts")
    manifest["counts"] = counts if isinstance(counts, dict) else {}
    return manifest


def _safe_target(base: Path, relative: str) -> Path | None:
    """`base` 아래의 대상 경로. 밖으로 나가는 경로면 None (zip slip 방지)."""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
        return None
    if PurePosixPath(relative).is_absolute():
        return None
    target = base.joinpath(*parts)
    try:
        target.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return target


def _extract(archive: zipfile.ZipFile, member: str, target: Path, overwrite: bool) -> bool:
    """항목 하나를 푼다. 이미 있어서 건너뛰었으면 False."""
    if target.exists() and not overwrite:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(archive.read(member))
    return True


def merge_restored_settings(restored: AppSettings, current: AppSettings) -> AppSettings:
    """백업의 설정에 **지금 PC의 폴더 경로**를 얹은 사본.

    백업을 만든 PC의 `D:\\NAI\\results` 같은 경로를 그대로 쓰면, 없는 드라이브를 가리키는
    설정으로 앱이 뜬다. 값(옵션·프롬프트·해상도 등)만 가져오고 자리는 지금 것을 지킨다.
    """
    merged = restored.model_copy(deep=True)
    for name in _MACHINE_PATH_FIELDS:
        setattr(merged, name, getattr(current, name))
    merged.lmstudio.prompt_config_path = current.lmstudio.prompt_config_path
    return merged


def _read_settings(archive: zipfile.ZipFile) -> AppSettings | None:
    try:
        data = json.loads(archive.read(_SETTINGS_ARCNAME).decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return AppSettings.model_validate(migrate_settings_data(data))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as e:
        logger.warning("backup settings unreadable: %s", e)
        return None


def restore_backup(
    source: Path,
    locations: BackupLocations,
    sections: set[str] | None = None,
    *,
    overwrite: bool = False,
) -> BackupResult:
    """고른 구획을 지금 PC의 폴더로 푼다. `sections`가 None이면 전부.

    설정 구획은 파일로 쓰지 않고 `BackupResult.settings`로 돌려준다 (모듈 설명 참고).

    Raises:
        BackupError: 이 앱의 백업이 아니거나(`not_a_backup`) 풀다가 실패했을 때
            (`restore_failed`).
    """
    read_manifest(source)  # 형식 확인 — 아무 zip이나 풀지 않는다
    chosen = set(SECTIONS) if sections is None else set(sections)
    result = BackupResult()
    data_targets = {path.name: path for path in locations.data_files}
    try:
        with zipfile.ZipFile(source) as archive:
            for member in archive.namelist():
                if member.endswith("/") or member == MANIFEST_NAME:
                    continue
                section, target = _resolve_member(member, locations, data_targets)
                if section is None or section not in chosen:
                    continue
                if section == SECTION_SETTINGS:
                    continue  # 아래에서 한 번에 읽는다
                if target is None:
                    result.rejected += 1
                    logger.warning("backup entry outside its folder skipped: %s", member)
                    continue
                if _extract(archive, member, target, overwrite):
                    result._add(section)
                else:
                    result.skipped += 1
            if SECTION_SETTINGS in chosen:
                result.settings = _read_settings(archive)
                if result.settings is not None:
                    result._add(SECTION_SETTINGS)
    except (OSError, zipfile.BadZipFile) as e:
        raise BackupError("restore_failed", str(e)) from e
    return result


def _resolve_member(
    member: str,
    locations: BackupLocations,
    data_targets: dict[str, Path],
) -> tuple[str | None, Path | None]:
    """zip 항목이 속한 구획과 풀 자리. 모르는 항목이면 (None, None)."""
    if member == _SETTINGS_ARCNAME:
        return SECTION_SETTINGS, locations.settings_file
    if member == _ARENA_ARCNAME:
        return SECTION_ARENA, locations.arena_file
    for section, prefix in _FOLDER_PREFIX.items():
        if member.startswith(prefix):
            return section, _safe_target(locations.folder(section), member[len(prefix) :])
    if member.startswith(_DATA_PREFIX):
        # 이름으로만 짝짓는다 — 백업이 아는 파일이 아니면 풀 자리가 없다.
        return SECTION_APP_DATA, data_targets.get(member[len(_DATA_PREFIX) :])
    return None, None


__all__ = [
    "FORMAT_NAME",
    "MANIFEST_NAME",
    "SECTIONS",
    "SECTION_APP_DATA",
    "SECTION_ARENA",
    "SECTION_ARTIST_COMBOS",
    "SECTION_PRESETS",
    "SECTION_SETTINGS",
    "SECTION_WILDCARDS",
    "BackupError",
    "BackupLocations",
    "BackupResult",
    "create_backup",
    "merge_restored_settings",
    "read_manifest",
    "restore_backup",
    "suggested_name",
]
