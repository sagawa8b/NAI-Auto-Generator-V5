"""WD14 ONNX-based image auto-tagging.

ONNX runtime은 선택적 의존성이다. 설치되지 않은 환경에서는
is_available == False를 반환하여 기능을 비활성화한다.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_MODEL_INPUT_SIZE = 448


class WD14Error(Exception):
    """WD14 태거 관련 모든 오류의 베이스.

    모델 손상, 추론 실패, 타임아웃 등을 표현한다.
    """


@dataclass(frozen=True)
class TagPrediction:
    """단일 태그 예측 결과."""

    tag: str
    confidence: float  # 0.0–1.0


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
    """ONNX-based image auto-tagging.

    Parameters
    ----------
    model_path : Path
        ONNX 모델 파일 경로.
    tags_path : Path
        태그 이름이 담긴 CSV 파일 경로. 첫 번째 열이 tag name.
    """

    def __init__(self, model_path: Path, tags_path: Path) -> None:
        self._model_path = model_path
        self._tags_path = tags_path
        self._session = None  # lazy-loaded ONNX InferenceSession
        self._tags: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if model file exists and onnxruntime can be imported."""
        if not self._model_path.exists():
            return False
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True

    def predict(
        self,
        image: Image.Image,
        threshold: float = 0.35,
        max_tags: int = 100,
    ) -> list[TagPrediction]:
        """Preprocess image, run ONNX inference, filter and sort results.

        Parameters
        ----------
        image : PIL.Image.Image
            Input image of any size/mode.
        threshold : float
            Minimum confidence to include a tag (0.01–1.0).
        max_tags : int
            Maximum number of tags to return.

        Returns
        -------
        list[TagPrediction]
            Tags sorted by confidence descending, filtered by threshold.

        Raises
        ------
        WD14Error
            On model corruption, inference failure, or other runtime errors.
        """
        # Clamp threshold to valid range
        threshold = max(0.01, min(1.0, threshold))

        # Ensure model is loaded
        self._ensure_session()

        # Load tags if not yet loaded
        if not self._tags:
            self._load_tags()

        # Preprocess
        input_array = self.preprocess(image)

        # Run inference
        try:
            input_name = self._session.get_inputs()[0].name
            output_name = self._session.get_outputs()[0].name
            results = self._session.run([output_name], {input_name: input_array})
        except Exception as exc:
            raise WD14Error(f"Inference failed: {exc}") from exc

        # Parse output
        predictions = self._parse_output(results[0], threshold, max_tags)
        return predictions

    def preprocess(self, image: Image.Image) -> np.ndarray:
        """Resize to 448×448 RGB, normalize to float32 [0,1].

        Parameters
        ----------
        image : PIL.Image.Image
            Input image of any size and mode.

        Returns
        -------
        np.ndarray
            Shape (1, 448, 448, 3), dtype float32, values in [0.0, 1.0].
        """
        # Convert to RGB if needed
        if image.mode != "RGB":
            image = image.convert("RGB")

        # Resize to model input size
        image = image.resize((_MODEL_INPUT_SIZE, _MODEL_INPUT_SIZE), Image.LANCZOS)

        # Convert to numpy array and normalize
        arr = np.array(image, dtype=np.float32) / 255.0

        # Add batch dimension: (448, 448, 3) → (1, 448, 448, 3)
        arr = np.expand_dims(arr, axis=0)

        return arr

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_session(self) -> None:
        """Lazily load the ONNX InferenceSession."""
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
        """Load tag names from CSV file.

        Expects a CSV with at least one column. The first column (or a column
        named 'name' or 'tag') is used as the tag name.
        """
        if not self._tags_path.exists():
            raise WD14Error(f"Tags file not found: {self._tags_path}")

        try:
            tags: list[str] = []
            with open(self._tags_path, encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)

                # Determine which column has the tag name
                name_col = 0
                if header:
                    header_lower = [h.strip().lower() for h in header]
                    if "name" in header_lower:
                        name_col = header_lower.index("name")
                    elif "tag" in header_lower:
                        name_col = header_lower.index("tag")
                    else:
                        # First row might be data, not header
                        if header[0].strip() and not header[0].strip().isdigit():
                            tags.append(header[name_col].strip())

                for row in reader:
                    if row and len(row) > name_col:
                        tag = row[name_col].strip()
                        if tag:
                            tags.append(tag)

            self._tags = tags
            logger.info("Loaded %d tags from %s", len(tags), self._tags_path)
        except Exception as exc:
            raise WD14Error(f"Failed to read tags file: {exc}") from exc

    def _parse_output(self, output: np.ndarray, threshold: float, max_tags: int) -> list[TagPrediction]:
        """Parse model output array into filtered, sorted TagPrediction list."""
        # output shape is typically (1, num_tags) or (num_tags,)
        scores = output.flatten()

        # Ensure we don't exceed available tags
        num_scores = min(len(scores), len(self._tags))

        # Build (tag, confidence) pairs above threshold
        predictions: list[TagPrediction] = []
        for i in range(num_scores):
            confidence = float(scores[i])
            if confidence >= threshold:
                predictions.append(TagPrediction(tag=self._tags[i], confidence=confidence))

        # Sort by confidence descending
        predictions.sort(key=lambda p: p.confidence, reverse=True)

        # Limit to max_tags
        return predictions[:max_tags]
