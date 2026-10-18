"""설정 저장소 — **시스템 설정 파일**(JSON) 하나.

시스템 설정 파일은 앱 설정 전체(폴더 경로·옵션·마지막 프롬프트 등)를 담는다. 파일 메뉴의
`설정 저장`(생성 설정만 담는 파일)과는 다른 것이다.

- 기본 위치: platformdirs 설정 디렉터리의 `settings_v5.json` (`default_settings_path`)
- 사용자가 다른 위치를 고를 수 있다. 고른 위치는 기본 설정 디렉터리의 작은 포인터 파일
  (`settings_v5_location.txt`)에 적어 두고, 시작할 때 그 파일을 먼저 본다
  (`system_settings_path`). 포인터가 가리키는 폴더가 사라졌으면(분리한 USB 등) 기본 위치로
  돌아간다 — 없는 드라이브에 쓰다가 종료 시 죽는 것보다 낫다.
- v0.13 이전의 `settings.json`만 있으면 그 내용을 읽는다 (파일은 지우지 않는다). 다음
  저장부터는 `settings_v5.json`에 쓴다.

cwd 상대경로에 의존하지 않는다. 파손된 파일은 기본값으로 복구하되
원본을 .broken으로 백업한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import platformdirs
from pydantic import ValidationError

from .schema import APP_NAME, CURRENT_SCHEMA_VERSION, AppSettings

logger = logging.getLogger(__name__)


def ensure_dirs(settings: AppSettings) -> None:
    """설정에 적힌 작업 폴더를 만들어 둔다.

    폴더가 없으면 와일드카드·아티스트 조합이 조용히 비어 있는 것처럼 동작해서
    "기능이 안 된다"로 보인다. 만들지 못해도 앱은 계속 떠야 하므로 로그만 남긴다.
    """
    for path in (
        settings.save_dir,
        settings.wildcards_dir,
        settings.presets_dir,
        settings.artist_combos_dir,
        settings.arena_dir,
        settings.wd14_dir,  # 사용자가 여기에 ONNX 모델을 넣어 두면 된다
    ):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("cannot create folder %s: %s", path, e)


#: 시스템 설정 파일의 기본 이름.
SETTINGS_FILENAME = "settings_v5.json"
#: v0.13 이전의 이름 — 새 이름의 파일이 없을 때만 읽는다.
LEGACY_SETTINGS_FILENAME = "settings.json"
#: 사용자가 고른 시스템 설정 파일 위치를 적어 두는 포인터 (기본 설정 디렉터리에 둔다).
LOCATION_FILENAME = "settings_v5_location.txt"


def config_dir() -> Path:
    """기본 설정 디렉터리. 테스트는 이 함수를 임시 폴더로 바꿔 끼운다."""
    return Path(platformdirs.user_config_dir(APP_NAME))


def default_settings_path() -> Path:
    return config_dir() / SETTINGS_FILENAME


def _location_file() -> Path:
    return config_dir() / LOCATION_FILENAME


def custom_settings_path() -> Path | None:
    """포인터에 적힌 위치 (없거나 비었거나 못 읽으면 None)."""
    try:
        text = _location_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def system_settings_path() -> Path:
    """지금 쓰는 시스템 설정 파일 경로 — 사용자가 고른 위치, 없으면 기본 위치."""
    custom = custom_settings_path()
    if custom is None:
        return default_settings_path()
    if not custom.parent.is_dir():
        logger.warning("settings location %s is unavailable — using the default location", custom)
        return default_settings_path()
    return custom


def set_system_settings_path(path: Path | None) -> None:
    """시스템 설정 파일 위치를 기억한다. None이나 기본 위치면 포인터를 지운다.

    파일 내용은 옮기지 않는다 — 호출하는 쪽이 새 위치에 먼저 `save_settings`한다.
    """
    location = _location_file()
    if path is None or Path(path) == default_settings_path():
        location.unlink(missing_ok=True)
        return
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(str(Path(path)), encoding="utf-8")


def migrate_settings_data(data: dict) -> dict:
    """설정 dict를 CURRENT_SCHEMA_VERSION으로 올린다. 순수 함수 (Req 9.7).

    - 기존 키는 하나도 건드리지 않는다 → 사용자 값 보존
    - 새 필드는 넣지 않는다 → pydantic 기본값이 채운다
    - 알 수 없는 키는 그대로 남긴다 (pydantic이 무시)
    - schema_version이 CURRENT보다 크면 경고만 남기고 그대로 둔다 (전방 호환)
    """
    migrated = dict(data)
    version = migrated.get("schema_version")
    version = version if isinstance(version, int) and not isinstance(version, bool) else 1
    if version < CURRENT_SCHEMA_VERSION:
        logger.info("migrating settings schema %s → %s", version, CURRENT_SCHEMA_VERSION)
        migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    elif version > CURRENT_SCHEMA_VERSION:
        logger.warning("settings schema %s is newer than supported %s", version, CURRENT_SCHEMA_VERSION)
    return migrated


def _legacy_fallback(path: Path) -> Path:
    """기본 위치에 새 이름의 파일이 없고 예전 `settings.json`만 있으면 그쪽을 읽는다."""
    if path == default_settings_path() and not path.exists():
        legacy = path.with_name(LEGACY_SETTINGS_FILENAME)
        if legacy.exists():
            logger.info("reading legacy settings file %s", legacy)
            return legacy
    return path


def load_settings(path: Path | None = None) -> AppSettings:
    path = _legacy_fallback(path or system_settings_path())
    if not path.exists():
        return AppSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = migrate_settings_data(data)
        return AppSettings.model_validate(data)
    except (json.JSONDecodeError, ValidationError, OSError) as e:
        logger.error("settings file broken (%s), falling back to defaults: %s", path, e)
        try:
            path.replace(path.with_suffix(".json.broken"))
        except OSError:
            pass
        return AppSettings()


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    path = path or system_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
