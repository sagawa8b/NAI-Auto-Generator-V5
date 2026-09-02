"""WD14 ONNX 자동 태깅 — 모델 관리(찾기·내려받기)와 추론.

전처리·태그 해석은 V4(`danbooru_tagger.py`)에서 검증된 절차를 그대로 따른다.
SmilingWolf의 WD 태거는 **BGR · 0–255 · 정사각 패딩** 입력을 받는다. 여기서 그
규칙을 지키지 않으면 모델은 돌지만 결과 태그가 엉뚱해진다.

onnxruntime과 numpy는 이 기능에서만 쓴다. 모듈을 import하는 것만으로 numpy를
요구하지 않도록(옵션 창은 모델 목록만 보려고 이 모듈을 읽는다) 무거운 import는
함수 안에서 한다.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from PIL import Image

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

#: 모델이 입력 크기를 알려 주지 않을 때 쓰는 값 (WD14 계열은 모두 448이다).
DEFAULT_MODEL_INPUT_SIZE = 448

#: 옵션 창에서 고르고 내려받을 수 있는 모델 (V4의 `LIST_MODEL`과 같은 목록, 최신순).
KNOWN_MODELS: tuple[str, ...] = (
    "wd-swinv2-tagger-v3",
    "wd-v1-4-moat-tagger-v2",
    "wd-v1-4-convnextv2-tagger-v2",
    "wd-v1-4-convnext-tagger-v2",
    "wd-v1-4-vit-tagger-v2",
    "wd-v1-4-convnext-tagger",
)

#: 모델 파일은 HuggingFace의 SmilingWolf 저장소에서 받는다 (V4와 같은 주소).
DOWNLOAD_URL = "https://huggingface.co/SmilingWolf/{model}/resolve/main/{file}"

#: 폴더에서 먼저 찾아보는 관습적인 파일 이름.
_PREFERRED_MODEL_NAME = "model.onnx"
_PREFERRED_TAGS_NAMES = ("selected_tags.csv", "tags.csv")

#: selected_tags.csv의 category 열 값.
_CATEGORY_GENERAL = "0"
_CATEGORY_CHARACTER = "4"
_CATEGORY_RATING = "9"

_DOWNLOAD_CHUNK = 1 << 16
_DOWNLOAD_TIMEOUT = 60


class WD14Error(Exception):
    """WD14 태거 관련 모든 오류의 베이스.

    모델 손상, 추론 실패, 내려받기 실패 등을 표현한다.
    """


class WD14DownloadCancelled(WD14Error):
    """사용자가 모델 내려받기를 취소했다."""


@dataclass(frozen=True)
class TagPrediction:
    """단일 태그 예측 결과."""

    tag: str
    confidence: float  # 0.0–1.0


# ---------------------------------------------------------------------------
# 모델 폴더 다루기
# ---------------------------------------------------------------------------


def runtime_error() -> str:
    """WD14 추론에 필요한 패키지를 쓸 수 있으면 "", 아니면 그 이유.

    모델 파일이 없는 것과 onnxruntime을 못 쓰는 것은 사용자가 할 일이 전혀 다르다
    (하나는 모델 내려받기, 하나는 설치 문제). 그래서 한 덩어리로 묶지 않고 따로 본다.
    """
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as e:  # ImportError 말고 DLL 적재 실패도 여기로 온다
        return str(e)
    return ""


def installed_models(directory: Path) -> list[str]:
    """폴더에 들어 있는 모델 이름(확장자 뺀 `.onnx` 파일 이름) — 이름순."""
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.onnx") if path.is_file())


def model_files(directory: Path, name: str) -> tuple[Path, Path]:
    """이름으로 정해지는 모델·태그 파일 경로 (`<name>.onnx`, `<name>.csv`).

    파일이 실제로 있는지는 보지 않는다 — 내려받을 위치를 정할 때도 쓴다.
    """
    stem = name[:-5] if name.endswith(".onnx") else name
    return directory / f"{stem}.onnx", directory / f"{stem}.csv"


def resolve_model_files(directory: Path, preferred: str = "") -> tuple[Path | None, Path | None]:
    """폴더에서 쓸 ONNX 모델과 태그 CSV를 고른다. 없으면 그 자리에 None.

    고르는 순서:

    1. `preferred`(옵션에서 고른 모델 이름)의 `<이름>.onnx` + `<이름>.csv`
    2. 관습적인 이름 — `model.onnx` + `selected_tags.csv`
    3. 폴더 안의 첫 번째 `.onnx` / `.csv` (이름순 — 같은 폴더면 늘 같은 파일)

    Parameters
    ----------
    directory : Path
        사용자가 옵션에서 지정한 WD14 모델 폴더.
    preferred : str
        옵션에서 고른 모델 이름. 빈 문자열이면 폴더를 훑어 고른다.

    Returns
    -------
    tuple[Path | None, Path | None]
        (모델 경로, 태그 CSV 경로).
    """
    if not directory.is_dir():
        return None, None

    if preferred:
        model, tags = model_files(directory, preferred)
        if model.is_file():
            return model, tags if tags.is_file() else _find_tags(directory, model)

    model = None
    conventional = directory / _PREFERRED_MODEL_NAME
    if conventional.is_file():
        model = conventional
    else:
        onnx_files = sorted(path for path in directory.glob("*.onnx") if path.is_file())
        model = onnx_files[0] if onnx_files else None

    return model, _find_tags(directory, model)


def _find_tags(directory: Path, model: Path | None) -> Path | None:
    """모델과 짝이 되는 태그 CSV. 모델과 이름이 같은 것 → 관습적인 이름 → 첫 번째 CSV."""
    if model is not None:
        sibling = model.with_suffix(".csv")
        if sibling.is_file():
            return sibling
    for name in _PREFERRED_TAGS_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    csv_files = sorted(path for path in directory.glob("*.csv") if path.is_file())
    return csv_files[0] if csv_files else None


def download_model(
    name: str,
    directory: Path,
    on_progress=None,
    should_cancel=None,
) -> tuple[Path, Path]:
    """모델 한 벌(`.onnx` + `.csv`)을 HuggingFace에서 폴더에 내려받는다.

    태그 CSV(수백 KB)를 먼저 받고, 오래 걸리는 모델 파일(수백 MB)에 대해서만
    진행률을 알린다. 받는 중에는 `.part`로 쓰다가 다 받은 뒤 이름을 바꾼다 —
    중간에 끊긴 파일이 "설치된 모델"로 보이면 안 되기 때문이다.

    Parameters
    ----------
    name : str
        `KNOWN_MODELS`에 있는 모델 이름.
    directory : Path
        받을 폴더. 없으면 만든다.
    on_progress : Callable[[int, int], None] | None
        (받은 바이트, 전체 바이트)로 불린다. 전체 크기를 모르면 0.
    should_cancel : Callable[[], bool] | None
        True를 돌려주면 받기를 멈추고 `WD14DownloadCancelled`를 던진다.

    Returns
    -------
    tuple[Path, Path]
        (모델 경로, 태그 CSV 경로).
    """
    if name not in KNOWN_MODELS:
        raise WD14Error(f"Unknown model: {name}")

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WD14Error(f"Cannot create model folder: {exc}") from exc

    model_path, tags_path = model_files(directory, name)
    _download_file(DOWNLOAD_URL.format(model=name, file="selected_tags.csv"), tags_path, None, should_cancel)
    _download_file(DOWNLOAD_URL.format(model=name, file="model.onnx"), model_path, on_progress, should_cancel)
    logger.info("downloaded WD14 model %s to %s", name, directory)
    return model_path, tags_path


def _download_file(url: str, destination: Path, on_progress, should_cancel) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT) as response:
            if response.status_code != 200:
                raise WD14Error(f"Download failed (HTTP {response.status_code}): {url}")
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            with open(partial, "wb") as f:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if should_cancel is not None and should_cancel():
                        raise WD14DownloadCancelled(f"Download cancelled: {url}")
                    if not chunk:
                        continue
                    f.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
    except WD14Error:
        partial.unlink(missing_ok=True)
        raise
    except (requests.RequestException, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise WD14Error(f"Download failed: {exc}") from exc

    try:
        partial.replace(destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise WD14Error(f"Cannot save {destination.name}: {exc}") from exc


#: 출력 길이 → 일반 태그 임계값 (General Tags Threshold). SmilingWolf의 wd-tagger 데모처럼
#: 임계값을 낮추면 더 많은 태그가 통과(길게), 높이면 적게 통과(짧게)한다. "중간"은 기존
#: 기본값 0.35다 (V4·기존 동작과 같다).
_LENGTH_THRESHOLDS = {
    "short": 0.5,
    "medium": 0.35,
    "long": 0.25,
}

#: 기본 일반 태그 임계값 (길이 "중간"과 같다).
DEFAULT_TAG_THRESHOLD = 0.35


def threshold_for_length(length: str) -> float:
    """출력 길이(short|medium|long)에 해당하는 일반 태그 임계값. 알 수 없으면 중간값."""
    return _LENGTH_THRESHOLDS.get(length, DEFAULT_TAG_THRESHOLD)


def append_tags_to_prompt(current_prompt: str, tags: list[str]) -> str:
    """선택한 태그를 프롬프트 뒤에 쉼표로 이어 붙인다.

    Qt와 무관한 문자열 처리라 core에 둔다 (전에는 `ui/wd14_dialog.py`의
    staticmethod였다 — 그 때문에 core 테스트가 PySide6를 목으로 흉내 내야 했다).

    Parameters
    ----------
    current_prompt : str
        현재 프롬프트 텍스트.
    tags : list[str]
        덧붙일 태그.

    Returns
    -------
    str
        태그가 붙은 프롬프트.
    """
    if not tags:
        return current_prompt

    joined = ", ".join(tags)
    if current_prompt.strip():
        return current_prompt.rstrip() + ", " + joined
    return joined


class WD14Tagger:
    """ONNX 모델로 이미지에서 태그를 뽑는다.

    Parameters
    ----------
    model_path : Path
        ONNX 모델 파일 경로.
    tags_path : Path
        태그 CSV 경로. SmilingWolf의 `selected_tags.csv`(tag_id,name,category,count)를
        쓰지만, category 열이 없는 CSV도 읽는다 (그때는 전부 일반 태그로 본다).
    """

    def __init__(self, model_path: Path, tags_path: Path) -> None:
        self._model_path = model_path
        self._tags_path = tags_path
        self._session = None  # lazy-loaded ONNX InferenceSession
        self._tags: list[str] = []
        self._categories: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def model_path(self) -> Path:
        """쓰고 있는 모델 파일 경로 (UI가 안내 문구에 그대로 보여 준다)."""
        return self._model_path

    @property
    def has_model(self) -> bool:
        return self._model_path.exists()

    @property
    def is_available(self) -> bool:
        """모델 파일이 있고 onnxruntime·numpy를 쓸 수 있는가."""
        return self.has_model and not runtime_error()

    def predict(
        self,
        image: Image.Image,
        threshold: float = 0.35,
        max_tags: int = 100,
        character_threshold: float = 0.85,
    ) -> list[TagPrediction]:
        """이미지에서 태그를 예측한다 (V4와 같은 기준).

        일반 태그는 `threshold`, 캐릭터 태그는 `character_threshold`를 넘어야 하고,
        등급(rating) 태그는 프롬프트에 쓸 것이 아니므로 아예 제외한다.

        Parameters
        ----------
        image : PIL.Image.Image
            크기·모드 상관 없음.
        threshold : float
            일반 태그 최소 확률 (0.01–1.0).
        max_tags : int
            돌려줄 최대 개수.
        character_threshold : float
            캐릭터 태그 최소 확률.

        Returns
        -------
        list[TagPrediction]
            확률 내림차순.

        Raises
        ------
        WD14Error
            모델 손상, 추론 실패 등.
        """
        threshold = max(0.01, min(1.0, threshold))
        character_threshold = max(0.01, min(1.0, character_threshold))

        self._ensure_session()
        if not self._tags:
            self._load_tags()

        input_meta = self._session.get_inputs()[0]
        input_array = self.preprocess(image, self.input_size())

        try:
            output_name = self._session.get_outputs()[0].name
            results = self._session.run([output_name], {input_meta.name: input_array})
        except Exception as exc:
            raise WD14Error(f"Inference failed: {exc}") from exc

        return self._parse_output(results[0], threshold, max_tags, character_threshold)

    def input_size(self) -> int:
        """모델이 요구하는 한 변 크기. 모델이 알려 주지 않으면 448."""
        self._ensure_session()
        shape = self._session.get_inputs()[0].shape
        size = shape[1] if len(shape) > 1 else None
        return size if isinstance(size, int) and size > 0 else DEFAULT_MODEL_INPUT_SIZE

    def preprocess(self, image: Image.Image, target_size: int = DEFAULT_MODEL_INPUT_SIZE) -> np.ndarray:
        """WD14 모델이 받는 형태로 변환한다 (V4와 같은 절차).

        흰색으로 정사각 패딩 → LANCZOS 축소 → RGB를 BGR로 → float32 0–255 →
        배치 차원 추가. **0–1로 정규화하지 않는다** — 이 모델들은 0–255를 받는다.

        Parameters
        ----------
        image : PIL.Image.Image
            크기·모드 상관 없음.
        target_size : int
            모델 입력 한 변 크기.

        Returns
        -------
        np.ndarray
            (1, target_size, target_size, 3), float32, BGR, 0–255.
        """
        import numpy as np

        if image.mode == "RGBA":
            # 투명 부분은 흰색 위에 합성한다 (그냥 convert하면 검게 깔린다)
            canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
            canvas.alpha_composite(image)
            image = canvas.convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        side = max(width, height)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(image, ((side - width) // 2, (side - height) // 2))
        square = square.resize((target_size, target_size), Image.LANCZOS)

        array = np.asarray(square, dtype=np.float32)
        array = array[:, :, ::-1]  # RGB → BGR
        return np.expand_dims(array, axis=0)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_session(self) -> None:
        """ONNX InferenceSession을 필요할 때 만든다."""
        if self._session is not None:
            return

        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise WD14Error("onnxruntime is not installed. Install it with: pip install onnxruntime") from exc

        if not self._model_path.exists():
            raise WD14Error(f"Model file not found: {self._model_path}")

        try:
            self._session = ort.InferenceSession(
                str(self._model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise WD14Error(
                f"Failed to load ONNX model (file may be corrupted or incompatible): {exc}"
            ) from exc

    def _load_tags(self) -> None:
        """태그 CSV를 읽는다 — 이름과 분류(category)를 함께.

        SmilingWolf의 `selected_tags.csv`는 `tag_id,name,category,count`다. 분류가
        없는 CSV(첫 열이 태그 이름뿐인 형식)도 읽되, 그때는 전부 일반 태그로 본다.
        태그의 밑줄은 공백으로 바꾼다 — 프롬프트에 그대로 넣기 위해서다.
        """
        if not self._tags_path.exists():
            raise WD14Error(f"Tags file not found: {self._tags_path}")

        try:
            tags: list[str] = []
            categories: list[str] = []
            with open(self._tags_path, encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)

                name_col = 0
                category_col: int | None = None
                if header:
                    header_lower = [h.strip().lower() for h in header]
                    if "name" in header_lower:
                        name_col = header_lower.index("name")
                    elif "tag" in header_lower:
                        name_col = header_lower.index("tag")
                    else:
                        # 헤더가 아니라 첫 데이터 행일 수 있다
                        if header[0].strip() and not header[0].strip().isdigit():
                            tags.append(_normalize_tag(header[name_col]))
                            categories.append(_CATEGORY_GENERAL)
                    if "category" in header_lower:
                        category_col = header_lower.index("category")

                for row in reader:
                    if not row or len(row) <= name_col:
                        continue
                    tag = _normalize_tag(row[name_col])
                    if not tag:
                        continue
                    tags.append(tag)
                    if category_col is not None and len(row) > category_col:
                        categories.append(row[category_col].strip())
                    else:
                        categories.append(_CATEGORY_GENERAL)

            self._tags = tags
            self._categories = categories
            logger.info("Loaded %d tags from %s", len(tags), self._tags_path)
        except WD14Error:
            raise
        except Exception as exc:
            raise WD14Error(f"Failed to read tags file: {exc}") from exc

    def _parse_output(
        self,
        output: np.ndarray,
        threshold: float,
        max_tags: int,
        character_threshold: float,
    ) -> list[TagPrediction]:
        """모델 출력에 태그 이름을 붙이고 분류별 임계값으로 거른다."""
        scores = output.flatten()
        num_scores = min(len(scores), len(self._tags))

        predictions: list[TagPrediction] = []
        for i in range(num_scores):
            category = self._categories[i] if i < len(self._categories) else _CATEGORY_GENERAL
            if category == _CATEGORY_RATING:
                continue  # 등급 태그는 프롬프트에 넣을 것이 아니다 (V4와 같다)
            limit = character_threshold if category == _CATEGORY_CHARACTER else threshold
            confidence = float(scores[i])
            if confidence >= limit:
                predictions.append(TagPrediction(tag=self._tags[i], confidence=confidence))

        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions[:max_tags]


def _normalize_tag(raw: str) -> str:
    """CSV의 태그 이름을 프롬프트에 넣을 형태로 (밑줄 → 공백)."""
    return raw.strip().replace("_", " ")
