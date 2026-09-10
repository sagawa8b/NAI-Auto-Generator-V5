"""타입 설정 스키마 (pydantic v2).

QSettings의 문자열 지옥("True" == bool 변환 등) 대신 타입/기본값/버전이
스키마로 강제된다. schema_version은 향후 마이그레이션 훅의 기준.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs
from pydantic import BaseModel, Field

from ..arena.finale import DEFAULT_PER_COMBO, DEFAULT_TOP_N
from ..arena.models import (
    CURVE_DESCENDING,
    INSERT_PREFIX,
    WEIGHT_MODE_BALANCED,
    WEIGHT_STEP,
    ComboGenParams,
)

APP_NAME = "NAI-Auto-V5"
#: QSettings(창 크기·스플리터 폭 등 UI 상태)의 조직 이름.
#: 비워 두면 Windows 레지스트리 백엔드가 AccessError 상태가 되어 읽기도 쓰기도
#: 조용히 무시된다 — 반드시 QCoreApplication.setOrganizationName()으로 지정해야 한다.
ORG_NAME = "sagawa8b"

CURRENT_SCHEMA_VERSION = 5
DEFAULT_WORD_LIMIT = 20
CUSTOM_RESOLUTION_SLOTS = 6
QUICK_COUNT_SLOTS = 4
#: 생성 바의 퀵 매수 버튼 기본값 (V4.5의 Quick Generation 프리셋과 같은 자리)
DEFAULT_QUICK_COUNTS = (5, 10, 30, 200)


#: 동일 조건으로 다시 생성할 때의 동작.
#: "generate"    — 그대로 생성한다 (같은 이미지가 한 장 더 나온다)
#: "random_seed" — 시드만 새로 뽑아 생성한다 (기본)
#: "block"       — 경고만 하고 생성하지 않는다
DUPLICATE_ACTIONS = ("generate", "random_seed", "block")
DEFAULT_DUPLICATE_ACTION = "random_seed"


def duplicate_action(value: str) -> str:
    """알 수 없는 값(손으로 고친 settings.json)은 기본값으로 떨어뜨린다."""
    return value if value in DUPLICATE_ACTIONS else DEFAULT_DUPLICATE_ACTION


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


def default_arena_dir() -> Path:
    """그림체 아레나의 데이터 폴더 (`arena.json` + `images/`).

    결과 폴더와 나눠 둔 이유는 아레나가 뽑는 그림이 **습작**이기 때문이다 — 결과
    폴더에 섞이면 갤러리가 아레나 이미지로 뒤덮인다.
    """
    return default_data_dir() / "artist_arena"


def default_wd14_dir() -> Path:
    """WD14 태거가 모델(.onnx)과 태그 CSV를 찾는 기본 폴더."""
    return default_data_dir() / "wd14"


class GenerationDefaults(BaseModel):
    model: str = "naid5f"
    width: int = 832
    height: int = 1216
    #: 해상도 패널에서 고른 등급 ("Normal"/"Large"/"Wallpaper"/"Custom"). ""이면 지정 없음
    #: — 크기로 되짚는다. 크기만으로는 등급을 되살릴 수 없어서 따로 남긴다: 커스텀 행은
    #: 보통 Normal에도 있는 크기라, 다시 켰을 때 등급이 Normal로 되돌아가면 해상도 랜덤이
    #: 커스텀이 아닌 Normal 해상도를 뽑는다.
    resolution_group: str = ""
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
    manual_position_override: bool = False  # True = 1명일 때도 수동 위치 지정


class BatchSettings(BaseModel):
    count: int = 0  # 0 = 무한
    delay_seconds: float = 3.0  # 요청 간 최소 간격 (보수적 기본값)
    stop_on_anlas_error: bool = True
    #: 생성 바의 퀵 매수 버튼 4개. 누르면 그 매수로 바로 연속 생성이 시작된다.
    quick_counts: list[int] = Field(default_factory=lambda: list(DEFAULT_QUICK_COUNTS))
    #: True면 매 장마다 Wide/Square/Portrait 중 하나를 무작위로 골라 생성한다 (i2i/인페인팅 잠금 중엔 무시).
    random_resolution: bool = False
    #: True면 "세팅별 연속 생성"이 고른 세팅 파일을 순서대로 돌지 않고 매번 무작위로 고른다
    #: (같은 파일이 연달아 두 번 나오지는 않는다).
    random_settings_order: bool = False
    #: 직전에 만든 이미지와 요청이 완전히 같을 때(메타데이터를 불러온 뒤 그대로 다시
    #: 누른 경우) 어떻게 할지. DUPLICATE_ACTIONS 참고. 알 수 없는 값은 읽을 때
    #: duplicate_action()이 기본값으로 떨어뜨린다 — 여기서 막으면 손으로 고친
    #: settings.json 하나가 설정 전체를 날린다 (store.load_settings의 복구 규칙).
    duplicate_action: str = DEFAULT_DUPLICATE_ACTION


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
    #: 캐릭터 위치 지정 캔버스. 여기만 기본이 펼침이다 — 접힌 채로 뜨면 캐릭터를
    #: 두 명 넣었을 때 위치를 지정할 수 있다는 사실 자체가 보이지 않는다.
    position_panel_expanded: bool = True


class PromptFontSettings(BaseModel):
    """프롬프트·네거티브·캐릭터 프롬프트 입력란 전용 폰트 크기/색상 (V4 기능 이식).

    `emphasis_color`/`deemphasis_color`는 가중치 문법(`2::text::`, `-2::text::`)의
    강조/약화 색이다 — V4의 `high_emphasis_color`/`low_emphasis_color`에 대응.
    """

    size: int = 0  # 0 = 기본값(적용 안 함)
    color: str = ""  # "" = 기본값, 값이 있으면 "#rrggbb"
    emphasis_color: str = ""  # "" = 기본 고정색 (가중치 > 1.0, 예: "2::text::")
    deemphasis_color: str = ""  # "" = 기본 고정색 (가중치 < 1.0, 예: "-2::text::")


class ArenaSettings(BaseModel):
    """그림체 아레나의 조작값 — 조합 생성·월드컵·진화 화면에서 만지는 것 전부.

    아레나가 자기 설정을 따로 들고 있는 이유는, 원본 프로그램에서 "껐다 켜면 작가 수
    최소·최대 같은 걸 다시 만져 줘야 한다"는 불만이 가장 많았기 때문이다. 화면에서
    바꾼 값은 전부 여기에 남아 다음 실행에 그대로 복원된다.

    생성 파라미터(모델·스텝·샘플러)는 여기에 두지 않는다 — 메인 창의 현재 설정을
    그대로 쓴다. 다만 해상도만은 따로 둘 수 있게 했다: 아레나는 그림체만 보면 되는
    습작이라 작게 뽑아 크레딧을 아끼는 쪽이 낫다.
    """

    # ── 조합 생성 ──────────────────────────────────────────────────────
    min_artists: int = 3
    max_artists: int = 6
    weight_mode: str = WEIGHT_MODE_BALANCED
    weight_min: float = 0.8
    weight_max: float = 1.8
    #: 가중치를 이 폭의 배수로 맞춘다 (0.01~1.0). 0.05면 1.15·1.20·1.25처럼,
    #: 0.01이면 1.16·1.17처럼 잘게 나온다.
    weight_step: float = WEIGHT_STEP
    curve: str = CURVE_DESCENDING
    #: 프롬프트에 `artist:` 접두사를 붙일지.
    use_prefix: bool = True
    #: 작가 블록을 기본 프롬프트의 앞/뒤/`<artist>` 자리 중 어디에 넣을지.
    #: 작가 태그를 맨 앞에 쓰는 사람이 많아 기본은 앞이다.
    insert_position: str = INSERT_PREFIX
    #: `랜덤 조합 만들기` 한 번에 만들 조합 수.
    batch_size: int = 12

    # ── 생성 ───────────────────────────────────────────────────────────
    #: 아레나 전용 해상도. 0이면 메인 창의 현재 해상도를 따른다.
    width: int = 0
    height: int = 0
    #: 세션 시드. 0이면 아레나를 처음 돌릴 때 하나 뽑아 여기 적어 둔다.
    #: **모든 조합이 같은 시드를 쓴다** — 구도·포즈가 같아야 그림체 차이만 남는다.
    seed: int = 0
    #: 준비된(그림이 있고 아직 안 싸운) 조합이 이 수 아래로 내려가면 다음 묶음을 미리 뽑는다.
    prefetch_threshold: int = 4
    #: 월드컵에서 그림이 떨어지면 알아서 더 뽑을지. **기본은 꺼짐** — 이것은 실제로
    #: 크레딧을 쓰는 동작이라, 사용자가 켜지 않았는데 앱이 알아서 소모하면 안 된다.
    auto_prefetch: bool = False

    # ── 월드컵 ─────────────────────────────────────────────────────────
    #: 어떤 조합끼리 붙일지: "all" | "favorites" | "unsettled"
    league: str = "all"
    #: 대결 화면의 이미지 배율 (%). QHD 이상에서 작아 보인다는 지적이 있어 넣었다.
    image_scale: int = 100
    #: 월드컵 판정 단축키(방향키·Space·Delete)를 쓸지. 끄면 그 키들이 포커스가 있는
    #: 위젯(슬라이더·콤보 상자 등)으로 그냥 간다.
    match_shortcuts: bool = True

    # ── 진화 ───────────────────────────────────────────────────────────
    #: 교배 한 번에 만들 자식 수.
    children_per_run: int = 8
    #: 부모로 쓸 상위 조합 수.
    parent_pool: int = 20
    mutation_rate: float = 0.15
    weight_jitter: float = 0.15
    #: 일괄 정리 조건 — 이만큼 싸웠는데 이 점수 아래면 지울 후보로 본다.
    purge_min_matches: int = 5
    purge_elo: float = 960.0
    # ── 통계 결산 ──────────────────────────────────────────────────────
    #: 결산에 올릴 등수 (위에서부터 몇 개까지).
    finale_top_n: int = DEFAULT_TOP_N
    #: 조합 하나당 뽑을 장수.
    finale_per_combo: int = DEFAULT_PER_COMBO
    #: 전적이 없는 조합도 결산에 넣을지. 1000점은 실력이 아니라 '모름'이라 기본은 꺼짐.
    finale_include_unrated: bool = False
    #: 결산 순위를 사람 Elo 대신 LLM 판독 점수로 매길지. 켜면 판독한 조합만 점수순으로
    #: 뽑힌다 (사람 월드컵을 안 돌렸어도 LLM 추천 순서로 결산할 수 있다). 기본은 꺼짐.
    finale_by_judge: bool = False

    #: 조합을 지울 때 그림 파일까지 지울지. **기본 꺼짐** — 파일을 남겨 두면 실수로
    #: 지워도 `되돌리기`가 그림까지 온전히 되살린다. 남은 파일은 통계 탭의
    #: `안 쓰는 그림 정리`로 언제든 치울 수 있다.
    delete_images_with_combo: bool = False

    # ── LLM 그림체 판독 ─────────────────────────────────────────────────
    #: V4 그림체를 재현하려고, 로컬 VLM(LM Studio)에게 참조 이미지와의 유사도를 매기게
    #: 하는 기능의 설정. 연결(host)·타임아웃은 `AppSettings.lmstudio`를 그대로 쓴다 —
    #: 여기는 판정에만 필요한 값을 둔다.
    #: 참조(V4 그림체) 이미지 파일 경로들. 사용자가 판정 탭에서 등록한다.
    judge_reference_paths: list[str] = Field(default_factory=list)
    #: 판정에 쓸 VLM 식별자. 빈 문자열이면 LM Studio에 로드된 첫 모델을 쓴다.
    #: 프롬프트 생성용 모델과 다를 수 있어(비전 필요) 별도로 둔다.
    judge_model: str = ""
    #: 판정 시스템 프롬프트 override. 빈 문자열이면 내장 기본 프롬프트를 쓴다.
    #: 값이 있으면 기본을 **완전히 교체**한다 (LLM 태깅의 덧붙이기와 다르다) — 모델마다
    #: 잘 듣는 채점 지시가 달라, 점수 포화가 심하면 여기서 rubric을 바꿔 실험할 수 있다.
    judge_system_prompt: str = ""
    #: 한 판정에 넣을 참조 이미지 최대 장수. 컨텍스트(~20k) 안에서 안전한 상한.
    #: 초과분은 잘라내고 UI가 안내한다.
    judge_max_references: int = 5
    #: 판정이 끝난 뒤 상위 몇 개를 즐겨찾기로 표시할지 (후속 액션 기본값).
    judge_favorite_top_n: int = 5

    def combo_params(self) -> ComboGenParams:
        """조합 생성 로직(`core/arena`)이 받는 모양으로. 범위 정리는 그쪽이 한다."""
        return ComboGenParams(
            min_artists=self.min_artists,
            max_artists=self.max_artists,
            weight_mode=self.weight_mode,
            weight_min=self.weight_min,
            weight_max=self.weight_max,
            weight_step=self.weight_step,
            curve=self.curve,
            use_prefix=self.use_prefix,
            mutation_rate=self.mutation_rate,
            weight_jitter=self.weight_jitter,
        ).clamped()


class LMStudioSettings(BaseModel):
    """자연어 프롬프트 생성용 로컬 LLM(LM Studio) 연결 설정.

    LM Studio 앱이 서빙하는 로컬 LLM에 공식 파이썬 SDK로 붙어, 입력한 단어/문장/이미지를
    NovelAI V5 프롬프트로 바꿔 준다. `core/llm/lmstudio_client.py` 참고.
    """

    host: str = "localhost:1234"  # LM Studio 서버 host:port
    model: str = ""  # 마지막으로 고른 모델 식별자 ("" = 로드된 첫 모델 자동 사용)
    timeout_seconds: float = 120.0  # 동기 응답 타임아웃 (0 이하 = 무제한)
    system_prompt: str = ""  # "" = 스타일 기본 프롬프트. 값이 있으면 그 뒤에 덧붙는 추가 지시
    default_apply_mode: str = "append"  # "append" | "replace" — 결과를 프롬프트에 넣는 방식
    #: 출력 스타일 기본값. "natural"(서술형 자연어) | "danbooru"(쉼표 구분 태그).
    #: 모델이 두 형태를 오가는 편차를 없애기 위해 스타일마다 시스템 프롬프트를 다르게 쓴다.
    default_style: str = "natural"
    #: 프롬프트 어시스턴트 창이 열릴 때 기본 모드.
    #: "wd_tagger"(WD14 로컬) | "llm_tagger"(LM Studio 태거) | "llm_assistant"(이미지+지시 변형).
    default_mode: str = "llm_tagger"
    #: 출력 길이 기본값. "short" | "medium" | "long".
    #: LLM은 max_tokens+프롬프트 지시로, WD 태거는 일반 태그 임계값으로 번역된다.
    default_length: str = "medium"
    #: LLM에 보내는 지시문을 항목별로 담은 JSON 파일 경로 (사용자가 외부 편집기로 고친다).
    #: 빈 문자열이면 앱 데이터 폴더의 `llm_prompts.json`을 찾고, 그것도 없으면 내장 기본 문안.
    #: 형식은 `core/llm/prompt_config.py` 참고 — 적어 둔 항목만 기본값 위에 덮인다.
    prompt_config_path: str = ""


class AppSettings(BaseModel):
    schema_version: int = CURRENT_SCHEMA_VERSION
    language: str = "ko"
    save_dir: str = Field(default_factory=lambda: str(default_save_dir()))
    wildcards_dir: str = Field(default_factory=lambda: str(default_wildcards_dir()))
    presets_dir: str = Field(default_factory=lambda: str(default_presets_dir()))
    artist_combos_dir: str = Field(default_factory=lambda: str(default_artist_combos_dir()))
    #: 그림체 아레나 데이터 폴더 (`arena.json` + `images/`).
    arena_dir: str = Field(default_factory=lambda: str(default_arena_dir()))
    #: WD14 자동 태깅이 쓸 ONNX 모델·태그 CSV가 든 폴더 (옵션 → 태그에서 바꾼다).
    wd14_dir: str = Field(default_factory=lambda: str(default_wd14_dir()))
    #: 쓸 WD14 모델 이름 (`<이름>.onnx`). 빈 문자열이면 폴더에서 찾아 쓴다.
    wd14_model: str = ""
    #: 갤러리가 훑을 폴더. 빈 문자열이면 결과 폴더를 본다 — 보통은 그게 맞고,
    #: V4 시절 모아 둔 이미지를 볼 때만 따로 지정한다.
    gallery_dir: str = ""
    log_dir: str = ""  # 빈 문자열 = OS 표준 로그 위치
    tag_database_path: str = ""  # 태그 자동완성 DB 경로 (빈 문자열 = 앱에 동봉된 기본 DB)
    tag_autocomplete_enabled: bool = True  # False면 DB가 있어도 태그 후보를 제안하지 않는다
    #: 프롬프트에 `__`/`##`를 칠 때 와일드카드 **이름**을 제안할지. 태그와 스위치를 나눠 둔 이유는
    #: 읽는 곳이 다르기 때문이다 — 태그는 `tag_database_path`의 DB 파일, 와일드카드는
    #: `wildcards_dir` 폴더다. 한쪽을 꺼도 다른 쪽은 그대로 뜬다.
    wildcard_autocomplete_enabled: bool = True
    filename_template: str = "{datetime}_{seed}"
    #: 생성 이미지 저장 형식. "png"(기본, 원본 그대로) 또는 "webp"(무손실, 메타데이터는
    #: EXIF로 옮겨 재사용 가능 — core/metadata/save.py 참고).
    image_format: str = "png"
    prompt_word_limit: int = DEFAULT_WORD_LIMIT
    character_word_limit: int = DEFAULT_WORD_LIMIT
    debug_headers: bool = False  # V5 초기 rate-limit 헤더 관찰용
    debug_logging: bool = False  # 로그 뷰어에서 켜면 DEBUG 레벨로 기록
    show_image_source: bool = False  # i2i 패널은 보기 메뉴(F2)로 켤 때만 표시
    show_enhance: bool = False  # 강화(업스케일) 패널은 보기 메뉴(F4)로 켤 때만 표시
    #: 결과 이미지 위에 그 장의 프롬프트를 겹쳐 보여 준다 (보기 메뉴 F8, V4의 '프롬프트 결과 표시')
    show_result_overlay: bool = False
    measure_credit: bool = False  # V5 크레딧 소모량 측정 로그 (도구 메뉴)
    check_updates_on_start: bool = True  # 시작 시 새 버전 확인 (기타 메뉴에서 수동 확인도 가능)
    generation: GenerationDefaults = Field(default_factory=GenerationDefaults)
    batch: BatchSettings = Field(default_factory=BatchSettings)
    prompts: PromptState = Field(default_factory=PromptState)
    resolution: ResolutionOptions = Field(default_factory=ResolutionOptions)
    ui: UiState = Field(default_factory=UiState)
    prompt_font: PromptFontSettings = Field(default_factory=PromptFontSettings)
    lmstudio: LMStudioSettings = Field(default_factory=LMStudioSettings)
    arena: ArenaSettings = Field(default_factory=ArenaSettings)

    def log_dir_path(self) -> Path:
        """설정된 로그 디렉터리. 빈 문자열이면 OS 표준 위치."""
        return Path(self.log_dir) if self.log_dir.strip() else default_log_dir()

    def wd14_dir_path(self) -> Path:
        """WD14 모델 폴더. 빈 문자열이면 기본 위치."""
        return Path(self.wd14_dir) if self.wd14_dir.strip() else default_wd14_dir()

    def gallery_dir_path(self) -> Path:
        """갤러리가 훑을 폴더. 빈 문자열이면 결과 폴더."""
        return Path(self.gallery_dir) if self.gallery_dir.strip() else Path(self.save_dir)
