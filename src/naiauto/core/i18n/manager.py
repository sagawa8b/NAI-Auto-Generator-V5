"""다국어 관리자 — 구 i18n_manager.py의 Qt-free 이식.

변경점:
  - pyqtSignal → subscribe/unsubscribe 콜백 옵저버 (ui/qt_bridge가 Qt 시그널로 변환)
  - __new__ 싱글톤 / import-time 파일시스템 부작용 제거: 컴포지션 루트에서
    리소스 경로를 주입해 생성한다
  - 언어 설정 영속화는 settings 레이어의 몫 (여기서 QSettings에 쓰지 않는다)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "ko"


def default_languages_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "resources" / "languages"


class I18nManager:
    def __init__(
        self,
        languages_path: Path | None = None,
        language: str = DEFAULT_LANGUAGE,
        fallback_language: str = DEFAULT_LANGUAGE,
    ):
        self.languages_path = languages_path or default_languages_path()
        self.current_language = language
        self.fallback_language = fallback_language
        self.translations: dict[str, dict] = {}
        self.available_languages: dict[str, str] = {}
        self._subscribers: list[Callable[[str], None]] = []
        self.load_languages()

    def load_languages(self) -> None:
        self.translations.clear()
        self.available_languages.clear()
        if not self.languages_path.exists():
            logger.error("languages path not found: %s", self.languages_path)
            return
        for filepath in sorted(self.languages_path.glob("*.json")):
            lang_code = filepath.stem
            try:
                lang_data = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.error("failed to load language file %s: %s", filepath.name, e)
                continue
            self.translations[lang_code] = lang_data.get("translations", {})
            self.available_languages[lang_code] = lang_data.get("language_name", lang_code)
        logger.info("loaded languages: %s", sorted(self.available_languages))

    def subscribe(self, callback: Callable[[str], None]) -> None:
        """언어 변경 콜백 등록. callback(language_code)."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def set_language(self, language_code: str) -> bool:
        if language_code not in self.translations:
            return False
        self.current_language = language_code
        for callback in list(self._subscribers):
            try:
                callback(language_code)
            except Exception:
                logger.exception("i18n subscriber failed")
        return True

    def get_text(self, key_path: str, *args) -> str:
        """점(.)으로 구분된 키 경로의 번역 반환. 없으면 폴백 언어, 그래도 없으면 키 반환."""
        value = self._lookup(self.current_language, key_path)
        if value is None and self.current_language != self.fallback_language:
            value = self._lookup(self.fallback_language, key_path)
        if not isinstance(value, str):
            logger.warning("missing translation key: %s", key_path)
            return key_path
        if args:
            try:
                return value.format(*args)
            except (IndexError, KeyError):
                logger.warning("translation format failed (key: %s, args: %s)", key_path, args)
        return value

    def _lookup(self, language: str, key_path: str):
        value: object = self.translations.get(language, {})
        for key in key_path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    def get_available_languages(self) -> dict[str, str]:
        return dict(self.available_languages)
