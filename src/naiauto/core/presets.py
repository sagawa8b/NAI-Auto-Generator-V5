"""Generation presets — named parameter snapshots for quick configuration switching.

Storage: {data_dir}/presets/{name}.json
Each preset is a single JSON file; PresetStore provides CRUD over the directory.
Numeric fields are clamped to GENERATION_PARAMS ranges on load.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .validation import GENERATION_PARAMS

logger = logging.getLogger(__name__)

# Characters allowed in preset filenames (sanitized from the name field)
_SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PresetError(Exception):
    """Raised when a preset file cannot be loaded (malformed JSON, etc.)."""


class CharacterPromptPreset(BaseModel):
    """캐릭터 슬롯 1개의 프리셋 저장 형태 (settings.CharacterPromptState와 같은 모양)."""

    prompt: str = ""
    uc: str = ""
    center_x: float = 0.5
    center_y: float = 0.5


class GenerationPreset(BaseModel):
    """Named generation parameter snapshot."""

    name: str
    model: str
    width: int
    height: int
    steps: int
    cfg_scale: float
    cfg_rescale: float
    sampler: str
    scheduler: str
    quality_tags: bool = True
    uc_preset: str = "heavy"
    var_plus: bool = False
    prompt: str = ""
    negative_prompt: str = ""
    characters: list[CharacterPromptPreset] = Field(default_factory=list)
    use_coords: bool = True
    manual_position_override: bool = False


def _sanitize_filename(name: str) -> str:
    """Convert a preset name to a filesystem-safe filename (without extension)."""
    sanitized = _SAFE_FILENAME_RE.sub("_", name).strip().strip(".")
    if not sanitized:
        sanitized = "_preset"
    return sanitized


def _clamp(value: float | int, min_val: float | None, max_val: float | None) -> float | int:
    """Clamp a numeric value to [min_val, max_val]."""
    if min_val is not None and value < min_val:
        value = type(value)(min_val)
    if max_val is not None and value > max_val:
        value = type(value)(max_val)
    return value


def _clamp_preset_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Clamp numeric fields according to GENERATION_PARAMS specs."""
    clamped = dict(data)
    for field_name in ("steps", "cfg_scale", "cfg_rescale", "width", "height"):
        if field_name not in clamped:
            continue
        spec = GENERATION_PARAMS.get(field_name)
        if spec is None:
            continue
        value = clamped[field_name]
        if isinstance(value, (int, float)):
            clamped[field_name] = _clamp(value, spec.min, spec.max)
    return clamped


class PresetStore:
    """CRUD operations on preset JSON files."""

    def __init__(self, presets_dir: Path) -> None:
        self._dir = Path(presets_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        """Resolve a preset name to its file path."""
        return self._dir / f"{_sanitize_filename(name)}.json"

    def list_presets(self) -> list[str]:
        """Return sorted list of preset names (derived from filenames)."""
        names: list[str] = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                names.append(data.get("name", p.stem))
            except (json.JSONDecodeError, OSError):
                # Skip unreadable files
                logger.warning("Skipping unreadable preset file: %s", p.name)
        return names

    def load(self, name: str) -> GenerationPreset:
        """Load a preset by name. Clamps numeric fields to valid ranges.

        Raises PresetError if the file is missing, malformed, or fails validation.
        Unknown model keys are preserved with a warning.
        """
        path = self._path_for(name)
        if not path.exists():
            raise PresetError(f"Preset not found: {name!r} (expected at {path})")

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PresetError(f"Cannot read preset file: {path} — {exc}") from exc

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PresetError(f"Malformed JSON in preset file: {path.name} — {exc}") from exc

        if not isinstance(data, dict):
            raise PresetError(f"Preset file must contain a JSON object: {path.name}")

        # Warn about unknown model keys (but preserve them in data for future compat)
        known_fields = set(GenerationPreset.model_fields.keys())
        unknown_keys = set(data.keys()) - known_fields
        if unknown_keys:
            logger.warning(
                "Preset %r contains unknown fields (preserved): %s",
                name,
                ", ".join(sorted(unknown_keys)),
            )

        # Clamp numeric fields to GENERATION_PARAMS bounds
        data = _clamp_preset_fields(data)

        try:
            preset = GenerationPreset.model_validate(data)
        except Exception as exc:
            raise PresetError(f"Preset schema validation failed for {path.name}: {exc}") from exc

        return preset

    def save(self, preset: GenerationPreset) -> Path:
        """Save a preset to disk. Returns the written file path."""
        path = self._path_for(preset.name)
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            preset.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return path

    def rename(self, old_name: str, new_name: str) -> Path:
        """Rename a preset. Returns the new file path.

        Raises PresetError if old preset doesn't exist or new name conflicts.
        """
        old_path = self._path_for(old_name)
        if not old_path.exists():
            raise PresetError(f"Preset not found: {old_name!r}")

        new_path = self._path_for(new_name)
        if new_path.exists() and new_path != old_path:
            raise PresetError(f"A preset named {new_name!r} already exists")

        # Load, update name, save to new path, delete old
        preset = self.load(old_name)
        preset = preset.model_copy(update={"name": new_name})
        new_path.write_text(
            preset.model_dump_json(indent=2),
            encoding="utf-8",
        )
        if old_path != new_path:
            old_path.unlink()
        return new_path

    def delete(self, name: str) -> None:
        """Delete a preset file. Raises PresetError if it doesn't exist."""
        path = self._path_for(name)
        if not path.exists():
            raise PresetError(f"Preset not found: {name!r}")
        path.unlink()

    def exists(self, name: str) -> bool:
        """Check if a preset with the given name exists."""
        return self._path_for(name).exists()
