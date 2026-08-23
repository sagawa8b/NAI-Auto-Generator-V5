"""설정 저장소 — JSON 파일, platformdirs 설정 디렉터리.

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
    ):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("cannot create folder %s: %s", path, e)


def default_settings_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME)) / "settings.json"


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


def load_settings(path: Path | None = None) -> AppSettings:
    path = path or default_settings_path()
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
    path = path or default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
