"""인증 세션 — pst- 영구 토큰(권장) 또는 이메일/비밀번호 로그인.

구 nai_generator.py에서 argon2 해시 + /user/login 흐름만 발췌했다.
세션 건강도 휴리스틱/적응형 갱신 주기는 가져오지 않는다:
정책은 단순하게 "401을 만나면 1회 재인증 후 재시도, 그래도 실패면 AuthError 표면화".
"""

from __future__ import annotations

import logging
from base64 import urlsafe_b64encode
from hashlib import blake2b

import argon2.low_level
import requests

from .errors import AuthError, NetworkError
from .model_specs import BASE_URL

logger = logging.getLogger(__name__)


def argon_hash(email: str, password: str, size: int, domain: str) -> str:
    pre_salt = f"{password[:6]}{email}{domain}"
    blake = blake2b(digest_size=16)
    blake.update(pre_salt.encode())
    salt = blake.digest()
    raw = argon2.low_level.hash_secret_raw(
        password.encode(),
        salt,
        2,
        int(2000000 / 1024),
        1,
        size,
        argon2.low_level.Type.ID,
    )
    return urlsafe_b64encode(raw).decode()


class NAISession:
    """액세스 토큰 보유자. 재인증 가능 여부는 로그인 방식에 따라 다르다.

    - login_with_token(pst-...): 토큰이 만료되지 않으므로 재인증 = 그대로 재사용
    - login(email, password): 401 시 /user/login 재호출로 새 accessToken 발급
    """

    def __init__(self) -> None:
        self.access_token: str | None = None
        self._email: str | None = None
        self._password: str | None = None
        self._is_persistent_token = False

    @property
    def is_logged_in(self) -> bool:
        return self.access_token is not None

    @property
    def token(self) -> str:
        if self.access_token is None:
            raise AuthError("Not logged in.")
        return self.access_token

    def login_with_token(self, api_key: str) -> None:
        if not api_key or not api_key.startswith("pst-"):
            raise AuthError("Persistent API token must start with 'pst-'.")
        self.access_token = api_key
        self._is_persistent_token = True
        self._email = None
        self._password = None

    def login(self, email: str, password: str) -> None:
        access_key = argon_hash(email, password, 64, "novelai_data_access_key")[:64]
        try:
            resp = requests.post(f"{BASE_URL}/user/login", json={"key": access_key}, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise NetworkError(f"Login network error: {e}") from e
        if resp.status_code == 401:
            raise AuthError("Login failed (401). Check email/password.")
        try:
            data = resp.json()
        except ValueError as e:
            raise AuthError(f"Login failed: unexpected response (HTTP {resp.status_code})") from e
        if "accessToken" not in data:
            raise AuthError(f"Login failed (HTTP {resp.status_code}): {str(data)[:200]}")
        self.access_token = data["accessToken"]
        self._is_persistent_token = False
        self._email = email
        self._password = password
        logger.info("logged in via email/password")

    def reauthenticate(self) -> bool:
        """401을 만난 호출자가 부르는 1회 재인증. 성공 여부를 반환."""
        if self._is_persistent_token:
            # pst- 토큰은 만료 개념이 없다. 401이면 토큰 자체가 무효.
            return False
        if self._email and self._password:
            try:
                self.login(self._email, self._password)
                return True
            except (AuthError, NetworkError) as e:
                logger.warning("reauthentication failed: %s", e)
                return False
        return False

    def logout(self) -> None:
        self.access_token = None
        self._email = None
        self._password = None
        self._is_persistent_token = False
