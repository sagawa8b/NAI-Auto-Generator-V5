"""NAI 메타데이터 리더 — 구 naiinfo_getter.py의 재작성.

변경점:
  - 반환 계약 하나로 통일: dict 또는 None (구현의 None-vs-tuple 버그 제거)
  - V3 레거시 형태 대신 관대한(tolerant) 파싱: 알려진 키는 최선으로 뽑고
    원문 전체를 "raw"에 보존한다. V5의 Comment 스키마가 바뀌어도
    raw는 항상 살아남는다.
  - PNG: tEXt 청크 우선, 없으면 EXIF, 없으면 stealth(알파채널 LSB) 폴백.
  - WebP: EXIF UserComment에 save_raw_png()가 넣어 둔 텍스트 필드를 그대로 복원해
    PNG의 tEXt 청크와 같은 모양(_parse_text_fields)으로 넘긴다.

읽기 순서가 관대해야 하는 이유는 재인코딩된 PNG 때문이다. 웹에 한 번 올라갔다
내려온 PNG는 용량 최적화기(oxipng·pngcrush 류)를 거치면서 텍스트 청크가
**IDAT 뒤로 밀리거나** zTXt/iTXt로 바뀌거나, 아예 EXIF(eXIf 청크)로 옮겨 간다.
Pillow는 IDAT 뒤의 청크를 `Image.open()` 시점에는 읽지 않으므로(`load()`가
끝나야 `img.info`에 들어온다) 여는 것만으로는 정보가 없는 것처럼 보인다.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from .stealth import read_info_from_image_stealth

logger = logging.getLogger(__name__)

# NAI 정보가 실려 오는 텍스트 필드 이름 (대소문자는 재인코딩 과정에서 자주 바뀐다)
_COMMENT_KEYS = ("Comment", "Description")

# stealth LSB는 픽셀 값을 그대로 읽어야 의미가 있다 — 팔레트/그레이스케일 PNG는
# 이미 색이 재양자화된 뒤라 숨은 비트가 남아 있지 않고, pixels[x, y]가 튜플도
# 아니라서 리더가 TypeError로 죽는다.
_STEALTH_MODES = frozenset({"RGB", "RGBA"})


def read_metadata(source: str | Path | bytes) -> dict[str, Any] | None:
    """이미지에서 NAI 생성 메타데이터를 읽는다.

    Returns:
        {
          "origin": "text_chunk" | "exif" | "webp_exif" | "stealth",
          "prompt": str | None,
          "negative_prompt": str | None,
          "seed": int | None,
          "model": str | None,        # Source 필드 (예: "NovelAI Diffusion V4.5 ...")
          "comment": dict | None,     # Comment JSON 파싱 결과
          "raw": dict,                # 발견된 모든 텍스트 필드 원문
        }
        메타데이터가 전혀 없으면 None.
    """
    try:
        if isinstance(source, bytes):
            img = Image.open(io.BytesIO(source))
        else:
            img = Image.open(source)
    except Exception as e:
        logger.warning("cannot open image: %s", e)
        return None

    with img:
        # IDAT 뒤에 붙은 tEXt/zTXt/iTXt/eXIf 청크는 load()가 끝나야 img.info에 들어온다.
        # 재인코딩된 PNG(웹 업로드본)가 바로 이 모양이라, 이 한 줄이 없으면
        # 정보가 남아 있는데도 "없음"으로 보인다.
        try:
            img.load()
        except Exception as e:
            logger.warning("cannot decode image data (metadata may be partial): %s", e)

        is_webp = img.format == "WEBP"
        fields = {k: v for k, v in (img.info or {}).items() if isinstance(v, str)}

        if not _has_comment(fields):
            exif_fields = _read_exif_text_fields(img)
            if exif_fields:
                # 청크 값이 있으면 그쪽을 이긴다 — EXIF는 어디까지나 폴백이다.
                fields = {**exif_fields, **fields}
                if _has_comment(fields):
                    return _parse_text_fields(fields, origin="webp_exif" if is_webp else "exif")

        if _has_comment(fields):
            return _parse_text_fields(fields, origin="webp_exif" if is_webp else "text_chunk")

        stealth_raw = _read_stealth(img) if not is_webp else None

    if stealth_raw:
        try:
            data = json.loads(stealth_raw)
        except json.JSONDecodeError:
            return {
                "origin": "stealth",
                "prompt": None,
                "negative_prompt": None,
                "seed": None,
                "model": None,
                "comment": None,
                "raw": {"stealth": stealth_raw},
            }
        if isinstance(data, dict):
            stealth_fields = {k: v for k, v in data.items() if isinstance(v, str)}
            return _parse_text_fields(stealth_fields, origin="stealth")

    # 알려진 키는 없어도 텍스트 필드 자체가 있으면 원문은 돌려준다 — 스키마가
    # 낯설 뿐 정보는 남아 있을 수 있고, 대화상자가 "원본 메타데이터"로 보여 준다.
    if fields:
        return _parse_text_fields(fields, origin="text_chunk")

    return None


def _read_stealth(img: Image.Image) -> str | None:
    """알파/RGB LSB에 숨은 정보를 읽는다. 읽을 수 없는 형식이면 조용히 넘어간다."""
    if img.mode not in _STEALTH_MODES:
        return None
    try:
        return read_info_from_image_stealth(img)
    except Exception as e:
        logger.debug("stealth pnginfo read failed: %s", e)
        return None


def _has_comment(fields: dict[str, str]) -> bool:
    return any(_field(fields, key) for key in _COMMENT_KEYS)


def _field(fields: dict[str, str], name: str) -> str | None:
    """텍스트 필드를 대소문자 구분 없이 찾는다.

    최적화기·변환기를 거치면 `Comment`가 `comment`나 `COMMENT`로 바뀌어 오는 일이
    흔하다. 정확히 일치하는 이름을 먼저 보고, 없으면 소문자로 비교한다.
    """
    value = fields.get(name)
    if isinstance(value, str):
        return value
    lowered = name.lower()
    for key, candidate in fields.items():
        if key.lower() == lowered and isinstance(candidate, str):
            return candidate
    return None


# ── EXIF ─────────────────────────────────────────────────────────────

# UserComment(0x9286)·ImageDescription(0x010E)·XPComment(0x9C9C) 순으로 본다.
# 웹 서비스가 PNG를 다시 인코딩하면서 텍스트 청크를 EXIF로 옮길 때 쓰는 자리다.
_EXIF_USER_COMMENT = 0x9286
_EXIF_IMAGE_DESCRIPTION = 0x010E
_EXIF_XP_COMMENT = 0x9C9C


def _read_exif_text_fields(img: Image.Image) -> dict[str, str] | None:
    """EXIF에서 PNG 텍스트 필드와 같은 모양의 dict를 복원한다.

    두 가지 형태를 받는다:
      1. `{"Description": ..., "Comment": ...}` — save_raw_png()가 WebP로 옮길 때 쓰는 형태
      2. NAI Comment 본문(`{"prompt": ..., "uc": ..., "seed": ...}`) 자체
    """
    try:
        exif = img.getexif()
    except Exception as e:
        logger.debug("cannot read EXIF: %s", e)
        return None
    if not exif:
        return None

    candidates: list[Any] = []
    for tag in (_EXIF_USER_COMMENT, _EXIF_IMAGE_DESCRIPTION, _EXIF_XP_COMMENT):
        candidates.append(exif.get(tag))
    # 표준 EXIF는 UserComment를 Exif 서브 IFD에 둔다 (우리 WebP는 IFD0에 둔다).
    try:
        sub_ifd = exif.get_ifd(0x8769)
    except Exception:
        sub_ifd = None
    if sub_ifd:
        candidates.append(sub_ifd.get(_EXIF_USER_COMMENT))

    for candidate in candidates:
        text = _decode_exif_text(candidate)
        if not text:
            continue
        data = _loads_dict(text)
        if data is None:
            continue
        fields = {k: v for k, v in data.items() if isinstance(v, str)}
        if _has_comment(fields):
            return fields
        # Comment 본문이 그대로 실린 경우 — 한 겹 감싸서 같은 계약으로 돌려준다.
        if _looks_like_nai_comment(data):
            return {"Comment": json.dumps(data, ensure_ascii=False)}
    return None


def _decode_exif_text(value: Any) -> str | None:
    """EXIF 값에서 문자열을 뽑는다 (UserComment의 8바이트 문자코드 머리말 포함)."""
    if isinstance(value, str):
        return value.strip("\x00") or None
    if not isinstance(value, bytes):
        return None
    if value[:8] == b"UNICODE\x00":
        for encoding in ("utf-16-be", "utf-16-le", "utf-8"):
            try:
                return value[8:].decode(encoding).strip("\x00") or None
            except UnicodeDecodeError:
                continue
        return None
    if value[:8] in (b"ASCII\x00\x00\x00", b"\x00" * 8):
        value = value[8:]
    try:
        return value.decode("utf-8").strip("\x00") or None
    except UnicodeDecodeError:
        return None


def _loads_dict(text: str) -> dict | None:
    parsed = _loads_json(text)
    return parsed if isinstance(parsed, dict) else None


def _loads_json(text: str) -> Any:
    """JSON을 읽는다. 문자열이 한 번 더 감싸여 있으면 (이중 인코딩) 한 겹 더 벗긴다."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, str):
        try:
            return json.loads(parsed)
        except (json.JSONDecodeError, TypeError):
            return None
    return parsed


def _looks_like_nai_comment(data: dict) -> bool:
    return any(key in data for key in ("prompt", "uc", "steps", "sampler", "seed"))


# ── 텍스트 필드 → 계약 dict ───────────────────────────────────────────


def _parse_text_fields(fields: dict[str, str], origin: str) -> dict[str, Any]:
    comment: dict | None = None
    comment_raw = _field(fields, "Comment")
    if comment_raw:
        parsed = _loads_json(comment_raw)
        if isinstance(parsed, dict):
            comment = parsed
        else:
            logger.debug("Comment field is not a JSON object")

    prompt = None
    negative_prompt = None
    seed = None
    if comment:
        prompt = _first_str(comment, "prompt")
        negative_prompt = _first_str(comment, "uc", "negative_prompt")
        raw_seed = comment.get("seed")
        if isinstance(raw_seed, int):
            seed = raw_seed
    if prompt is None:
        prompt = _field(fields, "Description")

    return {
        "origin": origin,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "model": _field(fields, "Source"),
        "comment": comment,
        "raw": fields,
    }


def _first_str(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None
