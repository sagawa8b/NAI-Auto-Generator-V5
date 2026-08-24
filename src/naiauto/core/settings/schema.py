"""타입 설정 스키마 (pydantic v2).

QSettings의 문자열 지옥("True" == bool 변환 등) 대신 타입/기본값/버전이
스키마로 강제된다. schema_version은 향후 마이그레이션 훅의 기준.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs
from pydantic import BaseModel, Field

APP_NAME = "NAI-Auto-V5"

CURRENT_SCHEMA_VERSION = 2
DEFAULT_WORD_LIMIT = 20
CUSTOM_RESOLUTION_SLOTS = 6
QUICK_COUNT_SLOTS = 4
#: 생성 바의 퀵 매수 버튼 기본값 (V4.5의 Quick Generation 프리셋과 같은 자리)
DEFAULT_QUICK_COUNTS = (5, 10, 30, 200)


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def default_log_dir() -> Path:
    return Path(platformdirs.user_log_dir(APP_NAME))


def default_save_dir() -> Path:
    return default_data_dir() / "results"


def default_wildcards_dir() -> Path:
    return default_data_dir() / "wildcards"


def default_presets_dir() -> Path:
    return default_data_dir() / "presets"


def default_artist_combos_dir() -> Path:
    return default_data_dir() / "artist_combos"


class GenerationDefaults(BaseModel):
    model: str = "naid5f"
    width: int = 832
    height: int = 1216
    steps: int = 28
    cfg_scale: float = 5.0
    cfg_rescale: float = 0.0  # matches V5 ModelSpec default
    sampler: str = "k_euler_ancestral"
    scheduler: str = "karras"  # matches V5 ModelSpec default
    seed: int = -1  # -1 = 매 생성마다 랜덤
    quality_tags: bool = True
    uc_preset: str = "heavy"
    var_plus: bool = False


class CharacterPromptState(BaseModel):
    """캐릭터 슬롯 1개의 영속 상태 (CharacterCaption과 1:1)."""

    prompt: str = ""
    uc: str = ""
    center_x: float = 0.5
    center_y: float = 0.5


class PromptState(BaseModel):
    """마지막으로 입력한 프롬프트. 재시작 시 그대로 복원된다."""

    prompt: str = ""
    negative_prompt: str = ""
    characters: list[CharacterPromptState] = Field(default_factory=list)
    use_coords: bool = True  # False = AI 위치 선택


class BatchSettings(BaseModel):
    count: int = 0  # 0 = 무한
    delay_seconds: float = 3.0  # 요청 간 최소 간격 (보수적 기본값)
    stop_on_anlas_error: bool = True
    #: 생성 바의 퀵 매수 버튼 4개. 누르면 그 매수로 바로 연속 생성이 시작된다.
    quick_counts: list[int] = Field(default_factory=lambda: list(DEFAULT_QUICK_COUNTS))


class CustomResolution(BaseModel):
    """해상도 옵션의 커스텀 행 1개."""

    enabled: bool = False
    width: int = 1024
    height: int = 1024


def _default_customs() -> list[CustomResolution]:
    """6개 슬롯을 흔히 쓰는 크기로 채운다 (모두 enabled=False)."""
    sizes = ((832, 1216), (1216, 832), (1024, 1024), (896, 1152), (1152, 896), (1024, 1536))
    return [CustomResolution(width=w, height=h) for w, h in sizes]


class ResolutionOptions(BaseModel):
    enable_large: bool = False
    enable_wallpaper: bool = False
    customs: list[CustomResolution] = Field(default_factory=_default_customs)


class UiState(BaseModel):
    """Collapsible_Section 펼침 상태. 기본은 접힘."""

    ai_settings_expanded: bool = False


class AppSettings(BaseModel):
    schema_version: int = CURRENT_SCHEMA_VERSION
    language: str = "ko"
    save_dir: str = Field(default_factory=lambda: str(default_save_dir()))
    wildcards_dir: str = Field(default_factory=lambda: str(default_wildcards_dir()))
    presets_dir: str = Field(default_factory=lambda: str(default_presets_dir()))
    artist_combos_dir: str = Field(default_factory=lambda: str(default_artist_combos_dir()))
    #: 갤러리가 훑을 폴더. 빈 문자열이면 결과 폴더를 본다 — 보통은 그게 맞고,
    #: V4 시절 모아 둔 이미지를 볼 때만 따로 지정한다.
    gallery_dir: str = ""
    log_dir: str = ""  # 빈 문자열 = OS 표준 로그 위치
    tag_database_path: str = ""  # 태그 자동완성 DB 경로 (빈 문자열 = 앱에 동봉된 기본 DB)
    filename_template: str = "{datetime}_{seed}"
    #: 생성 이미지 저장 형식. "png"(기본, 원본 그대로) 또는 "webp"(무손실, 메타데이터는
    #: EXIF로 옮겨 재사용 가능 — core/metadata/save.py 참고).
    image_format: str = "png"
    prompt_word_limit: int = DEFAULT_WORD_LIMIT
    character_word_limit: int = DEFAULT_WORD_LIMIT
    debug_headers: bool = False  # V5 초기 rate-limit 헤더 관찰용
    debug_logging: bool = False  # 로그 뷰어에서 켜면 DEBUG 레벨로 기록
    show_image_source: bool = False  # i2i 패널은 보기 메뉴(F2)로 켤 때만 표시
    measure_credit: bool = False  # V5 크레딧 소모량 측정 로그 (도구 메뉴)
    check_updates_on_start: bool = True  # 시작 시 새 버전 확인 (기타 메뉴에서 수동 확인도 가능)
    generation: GenerationDefaults = Field(default_factory=GenerationDefaults)
    batch: BatchSettings = Field(default_factory=BatchSettings)
    prompts: PromptState = Field(default_factory=PromptState)
    resolution: ResolutionOptions = Field(default_factory=ResolutionOptions)
    ui: UiState = Field(default_factory=UiState)

    def log_dir_path(self) -> Path:
        """설정된 로그 디렉터리. 빈 문자열이면 OS 표준 위치."""
        return Path(self.log_dir) if self.log_dir.strip() else default_log_dir()

    def gallery_dir_path(self) -> Path:
        """갤러리가 훑을 폴더. 빈 문자열이면 결과 폴더."""
        return Path(self.gallery_dir) if self.gallery_dir.strip() else Path(self.save_dir)
