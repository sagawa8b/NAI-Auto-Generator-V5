"""결과 저장 — API가 준 PNG bytes를 저장한다.

PNG은 재인코딩 없이 그대로 쓴다. V4 앱은 PIL로 재저장하면서 NAI tEXt 청크를 잃고
stealth 메타데이터만 남는 문제가 있었다 — raw_bytes verbatim 저장이 그래서 원칙이다.

WebP는 무손실로 다시 인코딩해야 하므로 이 원칙에서 벗어난다. WebP는 PNG의 tEXt 같은
텍스트 청크가 없어서, 같은 정보를 JSON으로 묶어 EXIF UserComment에 옮겨 싣는다 —
naiinfo.read_metadata()가 이를 그대로 읽어 PNG와 동일하게 프롬프트·시드를 재사용할 수
있게 한다 (표준 EXIF 인코딩 프리픽스는 넣지 않는다 — 우리 쪽 리더만 읽으면 된다).

파일명 템플릿 토큰은 `TOKEN_NAMES`에 한 번만 적는다 — UI(토큰 도움말)와
설정 검증(`has_known_token`)이 모두 이 목록을 참조한다.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

TOKEN_NAMES: tuple[str, ...] = (
    "datetime",
    "date",
    "time",
    "prompt",
    "negative_prompt",
    "character",
    "seed",
    "model",
)
DEFAULT_WORD_LIMIT = 20
_MAX_STEM_LEN = 120

#: 지원하는 저장 형식. UI 콤보와 설정 검증이 이 목록을 그대로 쓴다.
IMAGE_FORMATS: tuple[str, ...] = ("png", "webp")
DEFAULT_IMAGE_FORMAT = "png"

SAMPLE_CONTEXT: dict[str, object] = {  # Req 3.9 — 미리보기용 고정 예시 값
    "seed": 1234567890,
    "prompt": "1girl dancing in the rain",
    "negative_prompt": "lowres, bad anatomy",
    "character": "red hair, blue dress",
    "model": "nai-diffusion-5-full",
}

#: 길이가 사용자 입력에 따라 크게 달라지는 토큰. 상한을 넘길 때 이들이 예산을 나눠 쓴다.
#: 날짜·시드·모델은 길이가 뻔해서 대상이 아니다.
_VARIABLE_TOKENS: tuple[str, ...] = ("prompt", "negative_prompt", "character")

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


def _sanitize_untruncated(name: str) -> str:
    """금지 문자 치환 + 앞뒤 정리까지만. 절단은 하지 않는다 (예산 계산이 이 길이를 본다)."""
    return _strip_edges("".join("_" if _is_forbidden_char(ch) else ch for ch in name))


def sanitize_filename(name: str) -> str:
    """금지 문자 → '_', 앞뒤 공백·점 제거, 120자 절단, 빈 결과는 'image' (Req 3.8).

    순서: 금지 문자 치환 → strip → 120자 절단 → 다시 strip → 빈 값이면 'image'.
    절단 뒤 한 번 더 strip하는 이유는 120번째 문자가 공백이나 점일 수 있기 때문이다.
    """
    name = _sanitize_untruncated(name)
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
        # 네거티브 프롬프트도 본문 프롬프트와 같은 단어 수 제한을 쓴다 — 별도 설정을 두지 않는다.
        "negative_prompt": limit_words(str(context.get("negative_prompt", "")), prompt_word_limit),
        # 첫 번째 캐릭터 프롬프트를 뽑는 책임은 호출자에 있다 (Req 3.3, 3.4).
        "character": limit_words(str(context.get("character", "")), character_word_limit),
        "seed": str(context.get("seed", "")),
        "model": str(context.get("model", "")),
    }


def _token_count(template: str, name: str) -> int:
    """템플릿에 `{name}` / `[name]`이 몇 번 등장하는지."""
    return sum(1 for m in _TOKEN_RE.finditer(template) if (m.group(1) or m.group(2)) == name)


def _render(template: str, values: Mapping[str, str]) -> str:
    """{token}과 [token]을 같은 값으로 치환한다."""

    def _replace(match: re.Match[str]) -> str:
        return values[match.group(1) or match.group(2)]

    return _TOKEN_RE.sub(_replace, template)


def _shorten(text: str, limit: int) -> str:
    """`limit`자 이내로 줄인다. 가능하면 단어 경계에서 자르고 끝의 구분자를 정리한다."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 단어 중간에서 끊긴 경우, 마지막 공백까지 되돌린다 (너무 많이 잃지 않을 때만)
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,_-")


def _fit_to_budget(template: str, values: dict[str, str]) -> dict[str, str]:
    """가변 토큰들이 길이 상한을 나눠 갖도록 값을 줄인 사본을 돌려준다.

    상한을 넘길 때만 부른다. 고정 토큰(날짜·시드·모델)이 쓰는 만큼을 먼저 빼고, 남는 예산을
    템플릿에 실제로 등장한 가변 토큰 자리들이 똑같이 나눠 쓴다.

    가변 토큰 자리가 하나뿐이면 나눠 쓸 상대가 없으므로 손대지 않는다 — 그 경우 예전처럼
    끝에서 잘리고, 기존 사용자의 파일명이 달라지지 않는다.
    """
    present = [name for name in _VARIABLE_TOKENS if _token_count(template, name)]
    # 같은 토큰이 여러 번 쓰였으면 그만큼 자리를 더 먹는다
    slots = sum(_token_count(template, name) for name in present)
    if slots < 2:
        # 나눠 쓸 상대가 없다 — 예전처럼 sanitize의 절단에 맡긴다 (기존 파일명 유지)
        return values

    # 가변 토큰을 모두 비운 뒤의 길이 = 고정 부분이 이미 쓰고 있는 자리
    skeleton = _render(template, {**values, **{name: "" for name in present}})
    budget = _MAX_STEM_LEN - len(_sanitize_untruncated(skeleton))
    if budget <= 0:
        return values  # 고정 부분만으로 이미 꽉 찼다 — sanitize의 하드 절단에 맡긴다

    share = budget // slots
    return {**values, **{name: _shorten(values[name], share) for name in present}}


def format_filename(
    template: str,
    context: dict[str, Any],
    now: datetime | None = None,
    *,
    prompt_word_limit: int = DEFAULT_WORD_LIMIT,
    character_word_limit: int = DEFAULT_WORD_LIMIT,
) -> str:
    """{token}과 [token]을 같은 값으로 치환한 뒤 sanitize (Req 3.7, 3.8).

    상한(`_MAX_STEM_LEN`)을 넘기면 **꼬리를 자르지 않고** 가변 토큰들이 상한을 나눠 쓰도록
    각각 줄인다. 그냥 잘라내면 뒤쪽 토큰이 통째로 사라져, 사용자가 템플릿에 넣은
    `{negative_prompt}` 자리가 빈 채로 나온다 (v0.3.1). 상한 안에 들어오는 파일명은
    이 경로를 타지 않으므로 결과가 달라지지 않는다.
    """
    values = token_values(
        context,
        now or datetime.now(),
        prompt_word_limit=prompt_word_limit,
        character_word_limit=character_word_limit,
    )

    rendered = _render(template, values)
    if len(_sanitize_untruncated(rendered)) > _MAX_STEM_LEN:
        rendered = _render(template, _fit_to_budget(template, values))
    return sanitize_filename(rendered)


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
    image_format: str = DEFAULT_IMAGE_FORMAT,
) -> Path:
    """API가 준 PNG 원본을 저장한다. 파일명 충돌 시 _1, _2… 접미사.

    image_format="png"(기본)이면 bytes를 그대로 쓴다. "webp"면 무손실로 다시
    인코딩하고, PNG의 텍스트 청크는 EXIF로 옮겨 싣는다 (모듈 docstring 참고).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = format_filename(
        template,
        context or {},
        prompt_word_limit=prompt_word_limit,
        character_word_limit=character_word_limit,
    )
    suffix = ".webp" if image_format == "webp" else ".png"
    path = out_dir / f"{stem}{suffix}"
    counter = 1
    while path.exists():
        path = out_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    path.write_bytes(_encode_webp(raw_bytes) if image_format == "webp" else raw_bytes)
    return path


def _encode_webp(png_bytes: bytes) -> bytes:
    """NAI PNG를 무손실 WebP로 변환하고, PNG 텍스트 필드를 EXIF UserComment에 옮긴다."""
    from PIL import Image
    from PIL.ExifTags import Base as ExifBase

    with Image.open(io.BytesIO(png_bytes)) as img:
        text_fields = {k: v for k, v in (img.info or {}).items() if isinstance(v, str)}
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        exif = Image.Exif()
        if text_fields:
            exif[ExifBase.UserComment.value] = json.dumps(text_fields, ensure_ascii=False).encode("utf-8")

        buffer = io.BytesIO()
        img.save(buffer, format="WEBP", lossless=True, exif=exif.tobytes() if text_fields else b"")
        return buffer.getvalue()
