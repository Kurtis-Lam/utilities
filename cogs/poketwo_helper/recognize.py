"""Lightweight Pokémon image inference using ONNX Runtime (no PyTorch/Transformers).

Place this file beside the bot's recognizer cog module. The trained model directory
is expected at <project-root>/models/pokemon_mnv3_large/ by default.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any


class PokemonRecognizer:
    """ONNX Runtime classifier compatible with the uploaded Discord cog.

    Required deployment packages:
        pip install onnxruntime numpy pillow

    aiohttp is only needed by identify_from_url and is normally already installed
    by the Discord bot.
    """

    def __init__(self, model_dir: str | Path | None = None) -> None:
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.session: Any = None
        self.labels: list[str] = []
        self.pokeinfo: dict[str, Any] = {}
        self.pokevars: set[str] = set()
        self.image_size = 224
        self.mean = (0.485, 0.456, 0.406)
        self.std = (0.229, 0.224, 0.225)
        self.background = (248, 248, 248)
        self.input_name = "images"
        self.output_name = "logits"
        self._loaded = False

    @staticmethod
    def _candidate_model_dirs() -> list[Path]:
        here = Path(__file__).resolve().parent
        candidates: list[Path] = []
        for parent in (here, *here.parents):
            candidates.append(parent / "models" / "pokemon_mnv3_large")
        candidates.append(Path.cwd() / "models" / "pokemon_mnv3_large")
        # Also accept a models folder directly next to this module.
        candidates.append(here / "models")
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.resolve())
            if key not in seen:
                unique.append(candidate)
                seen.add(key)
        return unique

    def _resolve_model_dir(self) -> Path:
        if self.model_dir is not None:
            return self.model_dir.resolve()
        for candidate in self._candidate_model_dirs():
            if (candidate / "model.onnx").is_file() and (candidate / "labels.json").is_file():
                return candidate.resolve()
        tried = "\n".join(f"  - {path}" for path in self._candidate_model_dirs())
        raise FileNotFoundError(
            "Trained model files were not found. Run train.py and copy its output directory to "
            "models/pokemon_mnv3_large. Searched:\n" + tried
        )

    def load_resources(self) -> None:
        """Load the ONNX graph and label metadata. Safe to call more than once."""
        if self._loaded:
            return

        try:
            import numpy as np  # noqa: F401
            import onnxruntime as ort
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Prediction dependencies are missing. Install only these inference packages: "
                "onnxruntime, numpy, Pillow. PyTorch is not required on the bot host."
            ) from exc

        model_dir = self._resolve_model_dir()
        config_path = model_dir / "config.json"
        labels_path = model_dir / "labels.json"
        model_path = model_dir / "model.onnx"
        if not model_path.is_file() or not labels_path.is_file():
            raise FileNotFoundError(f"Model directory is incomplete: {model_dir}")

        with labels_path.open("r", encoding="utf-8") as handle:
            labels_data = json.load(handle)
        if isinstance(labels_data, list):
            self.labels = [str(item) for item in labels_data]
        elif isinstance(labels_data, dict) and isinstance(labels_data.get("labels"), list):
            self.labels = [str(item) for item in labels_data["labels"]]
        else:
            raise ValueError(f"Invalid labels.json format: {labels_path}")
        if not self.labels:
            raise ValueError("The trained model has an empty labels list.")

        if config_path.is_file():
            with config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            self.image_size = int(config.get("image_size", self.image_size))
            self.mean = tuple(float(x) for x in config.get("mean", self.mean))
            self.std = tuple(float(x) for x in config.get("std", self.std))
            self.background = tuple(int(x) for x in config.get("background", self.background))
            self.input_name = str(config.get("input_name", self.input_name))
            self.output_name = str(config.get("output_name", self.output_name))

        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # A single inference thread keeps memory and CPU use predictable on shared hosting.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = True
        options.enable_cpu_mem_arena = True

        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        actual_inputs = self.session.get_inputs()
        actual_outputs = self.session.get_outputs()
        if not actual_inputs or not actual_outputs:
            raise RuntimeError("ONNX model has no input or output tensors.")
        self.input_name = actual_inputs[0].name
        self.output_name = actual_outputs[0].name
        self.pokevars = {label.casefold() for label in self.labels}
        self._loaded = True

    def _preprocess(self, image: Any) -> Any:
        import numpy as np
        from PIL import Image

        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (*self.background, 255))
        rgb = Image.alpha_composite(background, rgba).convert("RGB")
        rgb.thumbnail((self.image_size, self.image_size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (self.image_size, self.image_size), self.background)
        x = (self.image_size - rgb.width) // 2
        y = (self.image_size - rgb.height) // 2
        canvas.paste(rgb, (x, y))

        array = np.asarray(canvas, dtype=np.float32) / 255.0
        array = (array - np.asarray(self.mean, dtype=np.float32)) / np.asarray(self.std, dtype=np.float32)
        array = np.transpose(array, (2, 0, 1))[None, ...]
        return np.ascontiguousarray(array, dtype=np.float32)

    def identify_from_bytes(self, image_bytes: bytes) -> tuple[str, float]:
        """Predict from encoded PNG/JPEG/WebP bytes; returns (label, confidence 0..1)."""
        if not self._loaded:
            self.load_resources()
        from PIL import Image, UnidentifiedImageError
        import numpy as np
        import io

        if not image_bytes:
            raise ValueError("The downloaded image is empty.")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                tensor = self._preprocess(image)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f"Unsupported or invalid image: {exc}") from exc

        outputs = self.session.run([self.output_name], {self.input_name: tensor})
        logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if logits.size != len(self.labels):
            raise RuntimeError(
                f"Model output has {logits.size} classes but labels.json has {len(self.labels)} labels."
            )
        logits -= float(np.max(logits))
        probabilities = np.exp(logits)
        denominator = float(np.sum(probabilities))
        if not denominator or not np.isfinite(denominator):
            raise RuntimeError("Model returned invalid prediction scores.")
        probabilities /= denominator
        best_index = int(np.argmax(probabilities))
        return self.labels[best_index], float(probabilities[best_index])

    async def identify_from_url(self, http_session: Any, image_url: str) -> tuple[str, float]:
        """Download the complete Discord image and run CPU inference.

        Important: aiohttp StreamReader.read(n) may return fewer than n bytes as
        soon as some data is available. Reading one large chunk this way can
        therefore hand Pillow an incomplete JPEG/PNG. Iterate until EOF instead.
        """
        if not image_url:
            raise ValueError("Image URL is empty.")
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("aiohttp is required for URL-based recognition.") from exc

        max_bytes = 12 * 1024 * 1024
        timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=12)
        last_error: Exception | None = None

        # Retry once if the CDN connection ends mid-image or Pillow detects a
        # truncated file. Do not enable Pillow's global truncated-image mode:
        # that can silently turn damaged images into misleading predictions.
        for attempt in range(2):
            try:
                async with http_session.get(
                    image_url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PokemonRecognizer/1.0)"},
                ) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError("Image is too large (maximum 12 MB).")
                        except ValueError as exc:
                            if "too large" in str(exc):
                                raise
                            # Ignore malformed Content-Length; the streamed
                            # byte limit below remains authoritative.
                            pass

                    image_buffer = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        image_buffer.extend(chunk)
                        if len(image_buffer) > max_bytes:
                            raise ValueError("Image is too large (maximum 12 MB).")

                if not image_buffer:
                    raise ValueError("The downloaded image is empty.")

                try:
                    return await asyncio.to_thread(
                        self.identify_from_bytes, bytes(image_buffer)
                    )
                except ValueError as exc:
                    if "truncated" not in str(exc).casefold() or attempt == 1:
                        raise
                    last_error = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt == 1:
                    raise RuntimeError(
                        "Could not download the complete image from Discord. "
                        "Please try again or upload the image again."
                    ) from exc

            if attempt == 0:
                await asyncio.sleep(0.25)

        if last_error:
            raise ValueError(
                "Discord returned an incomplete image after retrying. "
                "Please try again or re-upload the image."
            ) from last_error
        raise RuntimeError("Image download failed unexpectedly.")

    def predict_topk_from_bytes(self, image_bytes: bytes, top_k: int = 5) -> list[tuple[str, float]]:
        """Optional debugging helper returning the highest-scoring labels."""
        if not self._loaded:
            self.load_resources()
        import io
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            tensor = self._preprocess(image)
        logits = np.asarray(self.session.run([self.output_name], {self.input_name: tensor})[0], dtype=np.float32).reshape(-1)
        logits -= float(np.max(logits))
        scores = np.exp(logits)
        scores /= max(float(scores.sum()), 1e-12)
        indices = np.argsort(scores)[::-1][: max(1, min(int(top_k), len(self.labels)))]
        return [(self.labels[int(i)], float(scores[int(i)])) for i in indices]


def format_name(name: str) -> str:
    """Human-readable display name while retaining a class's form descriptor."""
    value = str(name or "").strip().replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    if not value:
        return value
    words = value.title()
    words = re.sub(r"\bFlabebe\b", "Flabébé", words)
    special = {
        "Mr Mime": "Mr. Mime",
        "Mime Jr": "Mime Jr.",
        "Type Null": "Type: Null",
        "Porygon Z": "Porygon-Z",
        "Ho Oh": "Ho-Oh",
        "Farfetch D": "Farfetch'd",
        "Flabebe": "Flabébé",
        "Nidoran Female": "Nidoran♀",
        "Nidoran Male": "Nidoran♂",
    }
    return special.get(words, words)


def extract_pokemon_from_text(text: str) -> str | None:
    """Extract the caught Pokémon name from typical Pokétwo catch messages."""
    if not text:
        return None
    clean = str(text)
    clean = re.sub(r"<a?:[A-Za-z0-9_]+:\d+>", " ", clean)
    clean = re.sub(r"<@!?\d+>", " ", clean)
    clean = re.sub(r"<@&\d+>", " ", clean)
    clean = re.sub(r"[\\*_`~]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    patterns = (
        r"\byou caught a level\s+\d+\s+(.+?)(?:[!?.\n]|$)",
        r"\bcaught a level\s+\d+\s+(.+?)(?:[!?.\n]|$)",
        r"\byou caught\s+(?:an?\s+)?(.+?)(?:[!?.\n]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip(" \t\r\n:,-")
            candidate = re.sub(r"\s+(?:with|and)\s+.*$", "", candidate, flags=re.IGNORECASE).strip()
            if candidate:
                # Catch-message spelling may include accents (e.g. Flabébé); labels
                # are commonly stored in ASCII folder names such as "flabebe".
                import unicodedata
                candidate = "".join(
                    char for char in unicodedata.normalize("NFKD", candidate)
                    if not unicodedata.combining(char)
                )
                return candidate.casefold()
    return None
