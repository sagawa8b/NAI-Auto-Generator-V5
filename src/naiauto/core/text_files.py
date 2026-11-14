"""사용자가 직접 만든 텍스트 파일을 관대하게 읽는다 (Qt-free).

와일드카드·V4 설정 파일은 사용자가 메모장 같은 편집기로 만든다. Windows 메모장은 오랫동안
ANSI(한국어 Windows면 CP949)로 저장했고, "UTF-8"을 고르면 BOM을 붙인다. `encoding="utf-8"`로만
읽으면 앞의 것은 `UnicodeDecodeError`로 죽고, 뒤의 것은 첫 줄 앞에 보이지 않는 `\\ufeff`가 남는다.

읽는 순서: UTF-8(BOM 허용) → 옛 인코딩 후보들 → UTF-8로 읽되 깨진 글자는 대체 문자(U+FFFD)로.
마지막 단계가 있으므로 **인코딩 때문에 예외가 나는 일은 없다** — 파일 하나가 이상하다고 생성이
멈추는 것보다, 글자 몇 개가 깨진 채로라도 읽히는 편이 낫다 (`OSError`는 그대로 올린다).

옛 인코딩 후보의 순서는 시스템 로케일에 따라 다르다 (`_legacy_candidates`):

- 로케일이 한·중·일 다중 바이트 코덱(CP949·CP932·GBK 등)이면 **로케일 → CP949**. 일본어·중국어
  Windows에서 만든 파일은 그 로케일로 읽어야 하고, CP949를 먼저 대면 엉뚱한 한글로 "성공"한다.
- 그 밖(영어 Windows의 CP1252, UTF-8 로케일 등)이면 **CP949 → 로케일**. CP1252 같은 단일 바이트
  코덱은 어떤 바이트열이든 예외 없이 읽어 버려서, 먼저 대면 한글 파일이 `»¡°£`처럼 깨진 채
  통과한다. 한국어 사용자가 가장 많으므로 엄격한 CP949를 먼저 시도한다.
"""

from __future__ import annotations

import codecs
import locale
from pathlib import Path

#: 한국어 사용자가 가장 많다 — 로케일이 무엇이든 한 번은 시도한다.
_LEGACY_FALLBACK = "cp949"

#: 한·중·일 다중 바이트 코덱 (`codecs.lookup().name`으로 정규화한 이름). 엄격해서 맞지 않는
#: 바이트열은 대개 예외로 걸러진다 — 로케일이 이쪽이면 로케일을 먼저 믿는다.
_CJK_CODECS = frozenset(
    {"cp949", "euc_kr", "cp932", "shift_jis", "euc_jp", "gbk", "gb2312", "gb18030", "big5", "cp950"}
)
_UTF8_CODECS = frozenset({"utf-8", "utf-8-sig"})


def _codec_name(encoding: str | None) -> str | None:
    """인코딩 이름을 정규화한다 ("CP949"·"ms949" → "cp949"). 모르는 이름이면 None."""
    if not encoding:
        return None
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return None


def _legacy_candidates() -> list[str]:
    """UTF-8이 아닐 때 시도할 인코딩들 (순서의 이유는 모듈 docstring)."""
    system = _codec_name(locale.getpreferredencoding(False))
    if system is None or system in _UTF8_CODECS:
        return [_LEGACY_FALLBACK]
    if system == _LEGACY_FALLBACK:
        return [system]
    if system in _CJK_CODECS:
        return [system, _LEGACY_FALLBACK]
    return [_LEGACY_FALLBACK, system]


def decode_lenient(data: bytes) -> str:
    """바이트를 텍스트로. 어떤 입력에도 예외를 내지 않는다."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    for encoding in _legacy_candidates():
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_text_lenient(path: str | Path) -> str:
    """파일을 읽어 텍스트로 돌려준다. 못 여는 경우에만 `OSError`."""
    return decode_lenient(Path(path).read_bytes())


__all__ = ["decode_lenient", "read_text_lenient"]
