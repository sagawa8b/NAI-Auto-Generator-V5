"""API 계정 슬롯 — pst- 영구 토큰 여러 개를 저장해 두고 골라 쓴다.

계정을 여러 개 쓰는 사용자가 토큰을 매번 붙여 넣지 않아도 되게, 최대
`MAX_ACCOUNTS`개의 토큰을 키링에 따로 저장한다. 지금 로그인에 쓰는 토큰은
전과 같이 `credentials.TOKEN_KEY`에 있고(시작 시 자동 로그인이 그걸 읽는다),
슬롯은 "언제든 전환할 수 있는 후보 목록"이다.

Qt를 모른다 — 다이얼로그(`ui/accounts_dialog.py`)는 여기서 읽고 쓰기만 한다.
"""

from __future__ import annotations

from . import credentials

#: 등록할 수 있는 계정 수. 늘리면 다이얼로그 행도 그만큼 늘어난다.
MAX_ACCOUNTS = 4

#: 슬롯 키는 활성 토큰 키(`api_token`)와 겹치지 않게 접미사를 붙인다.
_SLOT_KEY_FORMAT = credentials.TOKEN_KEY + "_slot{number}"

TOKEN_PREFIX = "pst-"

__all__ = [
    "MAX_ACCOUNTS",
    "TOKEN_PREFIX",
    "active_index",
    "adopt_active_token",
    "delete_token",
    "is_valid_token",
    "load_tokens",
    "save_token",
    "slot_key",
]


def slot_key(index: int) -> str:
    """슬롯 번호(0부터)에 대응하는 키링 키."""
    if not 0 <= index < MAX_ACCOUNTS:
        raise IndexError(f"account slot out of range: {index}")
    return _SLOT_KEY_FORMAT.format(number=index + 1)


def is_valid_token(token: str) -> bool:
    """NovelAI 영구 API 토큰 형식인가 (`pst-`로 시작)."""
    return token.strip().startswith(TOKEN_PREFIX)


def load_tokens() -> list[str]:
    """슬롯 4칸을 순서대로 읽는다. 비어 있는 칸은 빈 문자열."""
    return [credentials.load_credential(slot_key(i)) for i in range(MAX_ACCOUNTS)]


def save_token(index: int, token: str) -> bool:
    """슬롯에 토큰을 저장한다. 키링을 쓸 수 없으면 False."""
    return credentials.save_credential(slot_key(index), token.strip())


def delete_token(index: int) -> None:
    credentials.delete_credential(slot_key(index))


def active_index(tokens: list[str], active_token: str) -> int | None:
    """지금 쓰는 토큰이 몇 번 슬롯인지. 어디에도 없으면 None."""
    if not active_token:
        return None
    for index, token in enumerate(tokens):
        if token and token == active_token:
            return index
    return None


def adopt_active_token(active_token: str) -> list[str]:
    """이미 쓰고 있는 토큰이 슬롯에 없으면 빈 칸 하나에 넣고, 슬롯 목록을 돌려준다.

    계정 관리 창을 처음 열었을 때 "지금 로그인한 계정"이 1번으로 보이게 하려는 것이다.
    빈 칸이 없으면 아무것도 하지 않는다 (사용자가 채운 슬롯을 덮어쓰지 않는다).
    """
    tokens = load_tokens()
    if not active_token or active_index(tokens, active_token) is not None:
        return tokens
    for index, token in enumerate(tokens):
        if not token:
            if save_token(index, active_token):
                tokens[index] = active_token
            break
    return tokens
