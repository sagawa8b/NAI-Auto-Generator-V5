"""인증 정보 저장 — OS Keyring 전용.

구 gui_credentials.py에서 QSettings 평문 fallback을 제거했다:
keyring을 쓸 수 없으면 저장하지 않는다 (실행 시마다 입력).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SERVICE = "NAI-Auto-V5"

try:
    import keyring
    import keyring.errors

    _KEYRING_OK = True
except Exception:  # keyring은 백엔드에 따라 import 자체가 실패할 수 있다
    _KEYRING_OK = False
    logger.warning("keyring unavailable — credentials will not be persisted")


def is_available() -> bool:
    return _KEYRING_OK


def save_credential(key: str, value: str | None) -> bool:
    """저장 성공 여부 반환. value가 비면 삭제."""
    if not value:
        delete_credential(key)
        return True
    if not _KEYRING_OK:
        return False
    try:
        keyring.set_password(_SERVICE, key, value)
        return True
    except Exception as e:
        logger.warning("keyring save failed (%s): %s", key, e)
        return False


def load_credential(key: str) -> str:
    if not _KEYRING_OK:
        return ""
    try:
        return keyring.get_password(_SERVICE, key) or ""
    except Exception as e:
        logger.warning("keyring read failed (%s): %s", key, e)
        return ""


def delete_credential(key: str) -> None:
    if not _KEYRING_OK:
        return
    try:
        keyring.delete_password(_SERVICE, key)
    except Exception:
        pass
