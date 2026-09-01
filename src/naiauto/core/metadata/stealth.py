"""Stealth PNG info 리더 — 알파채널/RGB LSB에 숨겨진 메타데이터 추출.

구 stealth_pnginfo.py를 그대로 이식 (원 출처:
https://github.com/neggles/sd-webui-stealth-pnginfo/). 리더 전용.
"""

from __future__ import annotations

import gzip
import logging

from PIL import Image

logger = logging.getLogger(__name__)


def read_info_from_image_stealth(image: Image.Image) -> str | None:
    width, height = image.size
    pixels = image.load()

    has_alpha = image.mode == "RGBA"
    mode = None
    compressed = False
    binary_data = ""
    buffer_a = ""
    buffer_rgb = ""
    index_a = 0
    index_rgb = 0
    sig_confirmed = False
    confirming_signature = True
    reading_param_len = False
    reading_param = False
    read_end = False
    param_len = 0
    for x in range(width):
        for y in range(height):
            if has_alpha:
                r, g, b, a = pixels[x, y]
                buffer_a += str(a & 1)
                index_a += 1
            else:
                r, g, b = pixels[x, y]
            buffer_rgb += str(r & 1)
            buffer_rgb += str(g & 1)
            buffer_rgb += str(b & 1)
            index_rgb += 3
            if confirming_signature:
                if index_a == len("stealth_pnginfo") * 8:
                    decoded_sig = bytearray(
                        int(buffer_a[i : i + 8], 2) for i in range(0, len(buffer_a), 8)
                    ).decode("utf-8", errors="ignore")
                    if decoded_sig in {"stealth_pnginfo", "stealth_pngcomp"}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = "alpha"
                        if decoded_sig == "stealth_pngcomp":
                            compressed = True
                        buffer_a = ""
                        index_a = 0
                    else:
                        read_end = True
                        break
                elif index_rgb == len("stealth_pnginfo") * 8:
                    decoded_sig = bytearray(
                        int(buffer_rgb[i : i + 8], 2) for i in range(0, len(buffer_rgb), 8)
                    ).decode("utf-8", errors="ignore")
                    if decoded_sig in {"stealth_rgbinfo", "stealth_rgbcomp"}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = "rgb"
                        if decoded_sig == "stealth_rgbcomp":
                            compressed = True
                        buffer_rgb = ""
                        index_rgb = 0
                    elif not has_alpha:
                        # 알파 채널이 없으면 서명을 확인할 곳은 RGB뿐이다. 여기서 어긋났으면
                        # 숨겨진 데이터가 없는 것이므로 남은 픽셀을 훑을 이유가 없다.
                        # (원본 이식본에는 이 탈출구가 없어서, 평범한 RGB PNG 한 장을 읽는 데
                        #  전 픽셀을 도는 파이썬 루프가 돌았다 — 1024×1536이면 수십 초다.)
                        read_end = True
                        break
            elif reading_param_len:
                if mode == "alpha":
                    if index_a == 32:
                        param_len = int(buffer_a, 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_a = ""
                        index_a = 0
                else:
                    if index_rgb == 33:
                        pop = buffer_rgb[-1]
                        buffer_rgb = buffer_rgb[:-1]
                        param_len = int(buffer_rgb, 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_rgb = pop
                        index_rgb = 1
            elif reading_param:
                if mode == "alpha":
                    if index_a == param_len:
                        binary_data = buffer_a
                        read_end = True
                        break
                else:
                    if index_rgb >= param_len:
                        diff = param_len - index_rgb
                        if diff < 0:
                            buffer_rgb = buffer_rgb[:diff]
                        binary_data = buffer_rgb
                        read_end = True
                        break
            else:
                # impossible
                read_end = True
                break
        if read_end:
            break
    if sig_confirmed and binary_data != "":
        byte_data = bytearray(int(binary_data[i : i + 8], 2) for i in range(0, len(binary_data), 8))
        try:
            if compressed:
                return gzip.decompress(bytes(byte_data)).decode("utf-8")
            return byte_data.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error("Error decoding stealth PNG info: %s", e)
            return None

    return None
