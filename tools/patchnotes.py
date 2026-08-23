"""PATCHNOTES.md에서 한 버전의 절을 뽑는다 (릴리스 워크플로가 본문으로 쓴다).

    python tools/patchnotes.py v0.2.2     # 해당 절 출력, 없으면 종료 코드 1
    python tools/patchnotes.py            # naiauto.__version__ 기준

절 형식은 V4의 PATCHNOTES.md와 같다:

    ## v0.2.2 (2026-08-23)

    ### 🐛 버그 수정
    ...

Qt도 프로젝트 패키지도 import하지 않는다 — 러너에서 설치 없이 돌 수 있어야 한다
(버전 인자는 워크플로가 넘긴다).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PATCHNOTES = Path(__file__).resolve().parent.parent / "PATCHNOTES.md"

#: `## v0.2.2 (2026-08-23)` — 날짜는 있어도 되고 없어도 된다.
SECTION_RE = re.compile(r"^##\s+(?P<version>v[0-9][^\s(]*)\s*(?:\((?P<date>[^)]*)\))?\s*$")


def sections(text: str) -> dict[str, str]:
    """버전 → 본문. 파일에 나온 순서를 유지한다 (최신이 위)."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            version = match.group("version")
            if version in found:
                raise ValueError(f"중복된 절: {version}")
            current = found.setdefault(version, [])
            continue
        if line.startswith("## "):  # 버전이 아닌 다른 2단 제목 → 절 종료
            current = None
            continue
        if current is not None:
            current.append(line)
    return {version: _trim(body) for version, body in found.items()}


def _trim(lines: list[str]) -> str:
    """절 사이를 나누는 `---` 구분선은 본문이 아니다."""
    body = lines[:]
    while body and (not body[-1].strip() or body[-1].strip() == "---"):
        body.pop()
    return "\n".join(body).strip()


def extract(version: str, text: str | None = None) -> str:
    """`version` 절의 본문. 없거나 비어 있으면 `KeyError`."""
    if text is None:
        text = PATCHNOTES.read_text(encoding="utf-8")
    body = sections(text).get(version)
    if not body:
        raise KeyError(version)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="PATCHNOTES.md에서 한 버전의 절을 뽑는다")
    parser.add_argument("version", nargs="?", help="예: v0.2.2 (생략하면 naiauto.__version__)")
    args = parser.parse_args()

    version = args.version
    if not version:
        sys.path.insert(0, str(PATCHNOTES.parent / "src"))
        from naiauto import __version__

        version = f"v{__version__}"

    try:
        print(extract(version))
    except KeyError:
        print(f"PATCHNOTES.md에 {version} 절이 없습니다 — 릴리스 전에 추가하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
