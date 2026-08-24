"""NAI 메타데이터 리더 — 구 naiinfo_getter.py의 재작성.

변경점:
  - 반환 계약 하나로 통일: dict 또는 None (구현의 None-vs-tuple 버그 제거)
  - V3 레거시 형태 대신 관대한(tolerant) 파싱: 알려진 키는 최선으로 뽑고
    원문 전체를 "raw"에 보존한다. V5의 Comment 스키마가 바뀌어도
    raw는 항상 살아남는다.
  - PNG: tEXt 청크 우선, 없으면 stealth(알파채널 LSB) 폴백.
  - WebP: EXIF UserComment에 save_raw_png()가 넣어 둔 텍스트 필드를 그대로 복원해
    PNG의 tEXt 청크와 같은 모양(_parse_text_fields)으로 넘긴다.
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


def read_metadata(source: str | Path | bytes) -> dict[str, Any] | None:
    """PNG에서 NAI 생성 메타데이터를 읽는다.

    Returns:
        {
          "origin": "text_chunk" | "stealth",
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
        if img.format == "WEBP":
            fields = _read_webp_text_fields(img)
            return _parse_text_fields(fields, origin="webp_exif") if fields else None

        info = {k: v for k, v in (img.info or {}).items() if isinstance(v, str)}
        if info.get("Comment") or info.get("Description"):
            return _parse_text_fields(info, origin="text_chunk")

        stealth_raw = read_info_from_image_stealth(img)

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
            fields = {k: v for k, v in data.items() if isinstance(v, str)}
            return _parse_text_fields(fields, origin="stealth")

    return None


def _read_webp_text_fields(img: Image.Image) -> dict[str, str] | None:
    """save_raw_png()가 EXIF UserComment에 넣어 둔 PNG 텍스트 필드를 복원한다."""
    from PIL.ExifTags import Base as ExifBase

    try:
        raw = img.getexif().get(ExifBase.UserComment.value)
        if not raw:
            return None
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _parse_text_fields(fields: dict[str, str], origin: str) -> dict[str, Any]:
    comment: dict | None = None
    comment_raw = fields.get("Comment")
    if comment_raw:
        try:
            parsed = json.loads(comment_raw)
            if isinstance(parsed, dict):
                comment = parsed
        except json.JSONDecodeError:
            logger.debug("Comment field is not valid JSON")

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
        prompt = fields.get("Description")

    return {
        "origin": origin,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "model": fields.get("Source"),
        "comment": comment,
        "raw": fields,
    }


def _first_str(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None
