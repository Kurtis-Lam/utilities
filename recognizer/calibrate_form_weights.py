"""NumPy-only form calibration shared by the indexer and offline updater.

The bot runtime does not import this module. It only stores the calibrated
vectors produced here, so it gains no dependency or startup cost.
No PyTorch, Transformers, OpenCV, or GPU is required.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models"
VECTORS_PATH = MODEL_DIR / "pokemon_db_vectors.npy"
NAMES_PATH = MODEL_DIR / "pokemon_db_names.json"
CALIBRATION_PATH = MODEL_DIR / "pokemon_db_calibration.json"

# Strengths were selected using leave-one-out retrieval against the bundled
# stored vectors. Families not listed here already scored perfectly in that
# check and are deliberately left untouched.
FORM_STRENGTHS = {
    "flabb": 0.15,       # dataset label spelling for Flabébé
    "floette": 0.15,
    "florges": 0.15,
    "silvally": 0.15,
    "arceus": 0.15,
    "vivillon": 0.15,
    "deerling": 0.15,
    "sawsbuck": 0.15,
    "alcremie": 0.15,
    "unown": 0.15,
}

NEIGHBOURS = 4
SOFTMAX_TEMPERATURE = 0.02
PALAFIN_FINIZEN_STRENGTH = 0.15
EPSILON = 1e-12


def unit_rows(array: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, EPSILON)


def matches_family(name: str, family: str) -> bool:
    # Form names put the species token at the start or end, with modifiers
    # such as "blue flower" and "psychic" on either side.
    return family in name.lower().split()


def calibrate_vectors(input_vectors: np.ndarray, names: list[str]):
    """Return calibrated unit vectors plus a JSON-serializable calibration record."""
    vectors = np.asarray(input_vectors, dtype=np.float32).copy()
    if vectors.ndim != 2 or vectors.shape[0] != len(names):
        raise ValueError("Embedding matrix and class-name list do not match")
    if not np.isfinite(vectors).all():
        raise ValueError("Embedding matrix contains NaN or infinite values")

    vectors = unit_rows(vectors)
    by_name: dict[str, list[int]] = {}
    for row, name in enumerate(names):
        by_name.setdefault(name, []).append(row)

    # Palafin, Hero Palafin and Finizen are one retrieval family. The paired
    # update separates the two confusing learned classes; the Hero form is
    # still included in family-wide candidate expansion at runtime.
    pair_rows = 0
    rows_a = np.asarray(by_name.get("palafin", []), dtype=np.int64)
    rows_b = np.asarray(by_name.get("finizen", []), dtype=np.int64)
    if len(rows_a) >= 2 and len(rows_b) >= 2:
        mean_a = vectors[rows_a].mean(axis=0)
        mean_b = vectors[rows_b].mean(axis=0)
        mean_a /= max(float(np.linalg.norm(mean_a)), EPSILON)
        mean_b /= max(float(np.linalg.norm(mean_b)), EPSILON)
        direction = mean_a - mean_b
        direction /= max(float(np.linalg.norm(direction)), EPSILON)
        vectors[rows_a] += PALAFIN_FINIZEN_STRENGTH * direction
        vectors[rows_b] -= PALAFIN_FINIZEN_STRENGTH * direction
        vectors[rows_a] = unit_rows(vectors[rows_a])
        vectors[rows_b] = unit_rows(vectors[rows_b])
        pair_rows = int(len(rows_a) + len(rows_b))

    applied: dict[str, dict[str, float | int]] = {}
    for family, strength in FORM_STRENGTHS.items():
        class_names = [name for name in by_name if matches_family(name, family)]
        if len(class_names) < 2:
            continue

        class_rows = [np.asarray(by_name[name], dtype=np.int64) for name in class_names]
        centroids = []
        for rows in class_rows:
            centroid = vectors[rows].mean(axis=0)
            centroid /= max(float(np.linalg.norm(centroid)), EPSILON)
            centroids.append(centroid)
        centroids_array = np.stack(centroids)
        similarities = centroids_array @ centroids_array.T

        for class_id, rows in enumerate(class_rows):
            sibling_similarities = similarities[class_id].copy()
            sibling_similarities[class_id] = -2.0
            count = min(NEIGHBOURS, len(class_names) - 1)
            neighbour_ids = np.argsort(sibling_similarities)[-count:]
            neighbour_scores = sibling_similarities[neighbour_ids]
            weights = np.exp(
                (neighbour_scores - neighbour_scores.max()) / SOFTMAX_TEMPERATURE
            )
            weights /= max(float(weights.sum()), EPSILON)
            neighbour_centre = (centroids_array[neighbour_ids] * weights[:, None]).sum(axis=0)

            direction = centroids_array[class_id] - neighbour_centre
            direction /= max(float(np.linalg.norm(direction)), EPSILON)
            vectors[rows] += float(strength) * direction[None, :]
            vectors[rows] = unit_rows(vectors[rows])

        applied[family] = {
            "classes": len(class_names),
            "rows": int(sum(len(rows) for rows in class_rows)),
            "strength": float(strength),
        }

    if not applied and not pair_rows:
        raise RuntimeError("None of the configured form families matched the database labels")
    if not np.isfinite(vectors).all():
        raise ValueError("Calibration produced NaN or infinite values")
    if not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=2e-3):
        raise ValueError("Calibrated vectors are not unit-normalized")

    record = {
        "format": 2,
        "form_weight_method": "nearest_sibling_centroid_margin_adjustment",
        "form_weight_families": applied,
        "form_weight_pairs": (
            {"palafin+finizen": {"strength": PALAFIN_FINIZEN_STRENGTH, "rows": pair_rows}}
            if pair_rows else {}
        ),
        "form_weight_neighbours": NEIGHBOURS,
        "form_weight_softmax_temperature": SOFTMAX_TEMPERATURE,
        "runtime_dependencies_added": [],
        "note": (
            "Stored exemplar vectors only. The quantized CLIP ONNX encoder "
            "is unchanged; queries remain raw ONNX embeddings."
        ),
    }
    return vectors, record


def calibrate() -> None:
    if not VECTORS_PATH.is_file() or not NAMES_PATH.is_file():
        raise FileNotFoundError("Expected models/pokemon_db_vectors.npy and pokemon_db_names.json")

    metadata = {}
    if CALIBRATION_PATH.exists():
        metadata = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        if (
            metadata.get("form_weight_families")
            or metadata.get("form_weight_pairs")
            or metadata.get("updated_classes")
        ):
            raise RuntimeError(
                "Recognition weights have already been calibrated. Restore the "
                "original pokemon_db_vectors.npy and calibration metadata before "
                "applying this script again."
            )

    names = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    vectors = np.load(VECTORS_PATH).astype(np.float32)
    vectors, record = calibrate_vectors(vectors, names)
    np.save(VECTORS_PATH, vectors.astype(np.float16))
    metadata.update(record)
    CALIBRATION_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print("Calibrated form families:")
    for family, info in record["form_weight_families"].items():
        print(f"  {family}: {info['classes']} classes, {info['rows']} vectors, strength={info['strength']}")
    print(f"  Palafin/Finizen pair: {record['form_weight_pairs'].get('palafin+finizen', {}).get('rows', 0)} vectors")
    print(f"Wrote {VECTORS_PATH}")


if __name__ == "__main__":
    calibrate()
