"""Load and validate the fitted person-resolution logistic model."""

from __future__ import annotations

import math
import pickle
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

MODEL_FORMAT_VERSION = 1
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "artifacts" / "person_resolution_logreg.pkl"


class ModelArtifactError(ValueError):
    """The configured artifact does not match the inference contract."""


class _DataOnlyUnpickler(pickle.Unpickler):
    """Reject globals: the artifact must contain built-in data only."""

    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError(f"global {module}.{name} is forbidden in a model artifact")


@dataclass(frozen=True)
class LogisticModel:
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float

    def probability(self, features: Mapping[str, float]) -> float:
        if len(features) != len(self.feature_names) or set(features) != set(self.feature_names):
            raise ModelArtifactError("feature vector does not match the model feature schema")
        logit = self.intercept + sum(
            ((float(features[name]) - mean) / scale) * coefficient
            for name, mean, scale, coefficient in zip(
                self.feature_names,
                self.means,
                self.scales,
                self.coefficients,
                strict=True,
            )
        )
        if logit >= 0:
            return 1 / (1 + math.exp(-logit))
        exponent = math.exp(logit)
        return exponent / (1 + exponent)


def _numbers(payload: Mapping[str, Any], key: str, size: int) -> tuple[float, ...]:
    values = payload.get(key)
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) != size:
        raise ModelArtifactError(f"{key} must contain {size} numbers")
    try:
        converted = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ModelArtifactError(f"{key} must contain only numbers") from exc
    if not all(math.isfinite(value) for value in converted):
        raise ModelArtifactError(f"{key} contains a non-finite value")
    return converted


def _read_data(handle: BinaryIO) -> Any:
    try:
        return _DataOnlyUnpickler(handle).load()
    except (EOFError, pickle.UnpicklingError) as exc:
        raise ModelArtifactError("invalid or unsafe pickle model artifact") from exc


@lru_cache(maxsize=8)
def _load_model_cached(
    path: str,
    expected_features: tuple[str, ...],
    _mtime_ns: int,
    _size: int,
) -> LogisticModel:
    artifact_path = Path(path)
    try:
        with artifact_path.open("rb") as handle:
            payload = _read_data(handle)
    except OSError as exc:
        raise ModelArtifactError(f"cannot read model artifact: {artifact_path}") from exc
    if not isinstance(payload, Mapping):
        raise ModelArtifactError("model artifact must be a mapping")
    if payload.get("format_version") != MODEL_FORMAT_VERSION:
        raise ModelArtifactError(f"unsupported model artifact version: {payload.get('format_version')!r}")
    if payload.get("model_type") != "standardized_logistic_regression":
        raise ModelArtifactError(f"unsupported model type: {payload.get('model_type')!r}")

    feature_names = payload.get("feature_names")
    if not isinstance(feature_names, (list, tuple)) or not all(isinstance(name, str) for name in feature_names):
        raise ModelArtifactError("feature_names must be a list of strings")
    feature_names = tuple(feature_names)
    if feature_names != expected_features:
        raise ModelArtifactError("model feature schema does not match this PAUK version")

    means = _numbers(payload, "means", len(feature_names))
    scales = _numbers(payload, "scales", len(feature_names))
    if any(scale <= 0 for scale in scales):
        raise ModelArtifactError("model scales must be positive")
    coefficients = _numbers(payload, "coefficients", len(feature_names))
    try:
        intercept = float(payload["intercept"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelArtifactError("model intercept must be a number") from exc
    if not math.isfinite(intercept):
        raise ModelArtifactError("model intercept must be finite")
    return LogisticModel(feature_names, means, scales, coefficients, intercept)


def load_logistic_model(
    path: str | Path = DEFAULT_MODEL_PATH,
    expected_features: tuple[str, ...] = (),
) -> LogisticModel:
    """Load a trusted local artifact and validate it before inference."""
    if not expected_features:
        raise ModelArtifactError("expected feature schema is required")
    artifact_path = Path(path).resolve()
    try:
        metadata = artifact_path.stat()
    except OSError as exc:
        raise ModelArtifactError(f"cannot read model artifact: {artifact_path}") from exc
    return _load_model_cached(
        str(artifact_path),
        expected_features,
        metadata.st_mtime_ns,
        metadata.st_size,
    )
