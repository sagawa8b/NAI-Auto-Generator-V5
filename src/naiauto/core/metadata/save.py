"""결과 저장 — API가 준 PNG bytes를 재인코딩 없이 그대로 쓴다.

V4 앱은 PIL로 재저장하면서 NAI tEXt 청크를 잃고 stealth 메타데이터만 남는
문제가 있었다. 여기서는 raw_bytes verbatim 저장이 유일한 경로다.

파일명 템플릿 토큰은 `TOKEN_NAMES`에 한 번만 적는다 — UI(토큰 도움말)와
설정 검증(`has_known_token`)이 모두 이 목록을 참조한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

TOKEN_NAMES: tuple[str, ...] = ("datetime", "date", "time", "prompt", "character", "seed", "model")
DEFAULT_WORD_LIMIT = 20
_MAX_STEM_LEN = 120

SAMPLE_CONTEXT: dict[str, object] = {  # Req 3.9 — 미리보기용 고정 예시 값
    "seed": 1234567890,
    "prompt": "1girl dancing in the rain",
    "character": "red hair, blue dress",
    "model": "nai-diffusion-5-full",
}

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')
# {token} / [token] 을 한 번의 패스로 치환한다. 치환 결과를 다시 스캔하지 않으므로
# 프롬프트 안에 우연히 들어 있는 "[seed]" 같은 문자열은 토큰으로 해석되지 않는다.
_TOKEN_NAMES_ALT = "|".join(re.escape(name) for name in TOKEN_NAMES)
_TOKEN_RE = re.compile(rf"\{{({_TOKEN_NAMES_ALT})\}}|\[({_TOKEN_NAMES_ALT})\]")


def _is_forbidden_char(ch: str) -> bool:
    """파일 시스템 금지 문자이거나 출력 불가 문자(제어·포맷·구분자)인지."""
    return _INVALID_FS_CHARS.match(ch) is not None or not (ch == " " or ch.isprintable())


def _strip_edges(name: str) -> str:
    """앞뒤의 공백류·점을 제거한다 (Windows는 그런 이름을 거부한다)."""
    start, end = 0, len(name)
    while start < end and (name[start] == "." or name[start].isspace()):
        start += 1
    while end > start and (name[end - 1] == "." or name[end - 1].isspace()):
        end -= 1
    return name[start:end]


def sanitize_filename(name: str) -> str:
    """금지 문자 → '_', 앞뒤 공백·점 제거, 120자 절단, 빈 결과는 'image' (Req 3.8).

    순서: 금지 문자 치환 → strip → 120자 절단 → 다시 strip → 빈 값이면 'image'.
    절단 뒤 한 번 더 strip하는 이유는 120번째 문자가 공백이나 점일 수 있기 때문이다.
    """
    name = "".join("_" if _is_forbidden_char(ch) else ch for ch in name)
    name = _strip_edges(name)
    name = _strip_edges(name[:_MAX_STEM_LEN])
    return name or "image"


def limit_words(text: str, limit: int) -> str:
    """공백으로 분리한 앞쪽 limit개 단어를 단일 공백으로 이어 붙인다 (Req 3.5, 3.6).

    limit < 1은 1로 클램프한다.
    """
    return " ".join(text.split()[: max(1, limit)])


def token_values(
    context: Mapping[str, object],
    now: datetime,
    *,
    prompt_word_limit: int = DEFAULT_WORD_LIMIT,
    character_word_limit: int = DEFAULT_WORD_LIMIT,
) -> dict[str, str]:
    """토큰 이름 → 치환 문자열. TOKEN_NAMES 전부를 키로 가진다."""
    return {
        "datetime": now.strftime("%Y%m%d_%H%M%S"),
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "prompt": limit_words(str(context.get("prompt", "")), prompt_word_limit),
        # 첫 번째 캐릭터 프롬프트를 뽑는 책임은 호출자에 있다 (Req 3.3, 3.4).
        "character": limit_words(str(context.get("character", "")), character_word_limit),
        "seed": str(context.get("seed", "")),
        "model": str(context.get("model", "")),
    }


def format_filename(
    template: str,
    context: dict[str, Any],
    now: datetime | None = None,
    *,
    prompt_word_limit: int = DEFAULT_WORD_LIMIT,
    character_word_limit: int = DEFAULT_WORD_LIMIT,
) -> str:
    """{token}과 [token]을 같은 값으로 치환한 뒤 sanitize (Req 3.7, 3.8)."""
    values = token_values(
        context,
        now or datetime.now(),
        prompt_word_limit=prompt_word_limit,
        character_word_limit=character_word_limit,
    )

    def _replace(match: re.Match[str]) -> str:
        return values[match.group(1) or match.group(2)]

    return sanitize_filename(_TOKEN_RE.sub(_replace, template))


def has_known_token(template: str) -> bool:
    """{name} 또는 [name] 형태로 TOKEN_NAMES 중 하나라도 포함하는지 (Req 3.11)."""
    return _TOKEN_RE.search(template) is not None


def preview_filename(
    template: str,
    *,
    prompt_word_limit: int = DEFAULT_WORD_LIMIT,
    character_word_limit: int = DEFAULT_WORD_LIMIT,
    now: datetime | None = None,
) -> str:
    """SAMPLE_CONTEXT로 만든 미리보기 파일명 본체 (Req 3.9)."""
    return format_filename(
        template,
        dict(SAMPLE_CONTEXT),
        now,
        prompt_word_limit=prompt_word_limit,
        character_word_limit=character_word_limit,
    )


def save_raw_png(
    raw_bytes: bytes,
    out_dir: str | Path,
    template: str = "{datetime}_{seed}",
    context: dict[str, Any] | None = None,
    *,
    prompt_word_limit: int = DEFAULT_WORD_LIMIT,
    character_word_limit: int = DEFAULT_WORD_LIMIT,
) -> Path:
    """PNG 원본 bytes를 그대로 저장. 파일명 충돌 시 _1, _2… 접미사."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = format_filename(
        template,
        context or {},
        prompt_word_limit=prompt_word_limit,
        character_word_limit=character_word_limit,
    )
    path = out_dir / f"{stem}.png"
    counter = 1
    while path.exists():
        path = out_dir / f"{stem}_{counter}.png"
        counter += 1

    path.write_bytes(raw_bytes)
    return path
