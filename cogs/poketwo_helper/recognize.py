import os

# Prevent multi-threading memory expansion across CPU cores
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import asyncio
import gc
import hashlib
import io
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path

import aiohttp

# numpy and Pillow are imported on first use (see _load_heavy) so that merely
# loading the cog, which imports this module at startup, stays cheap.
np = None
Image = None
sliding_window_view = None
CLIP_MEAN = None
CLIP_STD = None
_RGB2XYZ = None
_WHITE = None

# -------------------------------------------------------------------
# Preprocessing constants (KEEP IDENTICAL to indexer.py)
# -------------------------------------------------------------------
TARGET_SIZE = 224
ZOOM_FACTOR = 0.85
BG_COLOR = (255, 255, 255)

# -------------------------------------------------------------------
# CLIP matching parameters (tune these)
# -------------------------------------------------------------------
SIM_CHUNK_ROWS = 2048
TOP_K_IMAGES = 40
CLASS_TOP_N = 3
CLASS_MAX_WEIGHT = 0.75
RERANK_MARGIN = 0.06
RERANK_MAX_CLASSES = 6

# -------------------------------------------------------------------
# Template verification parameters (tune these)
#
# CLIP cannot resolve tiny details (Palafin's chest heart, a Flabébé's flower
# colour), so when several classes are close the query is aligned with each
# class's stored template and compared pixel by pixel, concentrating on the
# pixels where the candidates actually differ from each other.
# -------------------------------------------------------------------
FRAME = 64                      # query frame side used for verification
COARSE = 32                     # frame side used for the fast alignment search
UPSCALE = FRAME // COARSE
SCALE_REL = (0.56, 0.62, 0.69, 0.76, 0.84, 0.93, 1.02)
K_MIN = FRAME // 2              # smallest plausible artwork size (pixels of the frame)
PREF_SCALE = 0.70               # artwork rarely covers less than this share of the frame
SCALE_PENALTY = 12.0            # fit-error penalty per unit of log(PREF_SCALE / scale)
NARROW_RANGE = 0.125            # centred search window (fraction of the frame)
WIDE_TRIGGER = 14.0             # poor narrow fit -> search the whole frame
FIT_TRUNC = 30.0                # per-pixel cap for alignment / fit errors (delta E)
COL_TRUNC = 40.0                # per-pixel cap for the discriminative comparison
TOLERANCE = 1                   # pixels of misalignment forgiven when comparing
FIT_GOOD = 7.0                  # fit error at which verification is fully trusted
FIT_BAD = 16.0                  # fit error at which verification is ignored
NULL_STEP = 0.08                # how far (share of the artwork) the 'wrong place' probes move
CONTRAST_LO = 4.0               # a real match fits this much better than a nudged one ...
CONTRAST_HI = 7.5               # ... fully trusted from here
DIFF_DEADZONE = 5.0             # template differences below this are noise
DIFF_MIN_WEIGHT = 12.0          # minimum discriminative evidence required
BETA_COL = 0.008                # score penalty per delta E lost head-to-head
BETA_FIT = 0.005                # score penalty per delta E of poorer shape/fit
FIT_DEADZONE = 2.0
ADJ_CAP = 0.20

# -------------------------------------------------------------------
# Runtime / resource parameters
# -------------------------------------------------------------------
CACHE_SIZE = 256
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
TRIM_EVERY = 64
DEBUG = os.environ.get("RECOGNIZER_DEBUG") == "1"


def _load_heavy():
    """Imports numpy / Pillow once and builds the constant arrays that need them."""
    global np, Image, sliding_window_view, CLIP_MEAN, CLIP_STD, _RGB2XYZ, _WHITE
    if np is not None:
        return

    import numpy
    from numpy.lib.stride_tricks import sliding_window_view as window_view
    from PIL import Image as pil_image

    np = numpy
    Image = pil_image
    sliding_window_view = window_view
    CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    _RGB2XYZ = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float32,
    )
    _WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float32)


def flatten_alpha(img):
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, BG_COLOR + (255,))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def center_square(img):
    width, height = img.size
    size = max(1, int(min(width, height) * ZOOM_FACTOR))
    left = (width - size) // 2
    top = (height - size) // 2
    return img.crop((left, top, left + size, top + size))


def make_frame(rgb):
    """Full centred square of the image at FRAME x FRAME (uint8), used for verification."""
    width, height = rgb.size
    side = max(1, min(width, height))
    left = (width - side) // 2
    top = (height - side) // 2
    square = rgb.crop((left, top, left + side, top + side))
    return np.asarray(
        square.resize((FRAME, FRAME), Image.Resampling.BOX), dtype=np.uint8
    )


def preprocess_image(img):
    """Returns (CHW float32 CLIP tensor, FRAME x FRAME x 3 uint8 verification frame)."""
    _load_heavy()
    rgb = flatten_alpha(img)
    square = center_square(rgb)

    resized = square.resize((TARGET_SIZE, TARGET_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(resized, dtype=np.float32)
    arr /= 255.0
    arr -= CLIP_MEAN
    arr /= CLIP_STD
    arr = np.ascontiguousarray(arr.transpose(2, 0, 1))

    return arr, make_frame(rgb)


# -------------------------------------------------------------------
# Template verification
# -------------------------------------------------------------------
def rgb_to_lab(rgb):
    c = np.asarray(rgb, dtype=np.float32) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _RGB2XYZ.T) / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    lab = np.stack(
        [
            116.0 * f[..., 1] - 16.0,
            500.0 * (f[..., 0] - f[..., 1]),
            200.0 * (f[..., 1] - f[..., 2]),
        ],
        axis=-1,
    )
    return lab.astype(np.float32)


def _template_at(tpl, k):
    """Template (S, S, 4 uint8) resized to k x k -> (Lab k x k x 3, alpha k x k)."""
    side = tpl.shape[0]
    im = Image.fromarray(np.ascontiguousarray(tpl))
    if k != side:
        method = Image.Resampling.BOX if k < side else Image.Resampling.BILINEAR
        im = im.resize((k, k), method)
    arr = np.asarray(im)
    return rgb_to_lab(arr[..., :3]), arr[..., 3].astype(np.float32) / 255.0


def _scale_cost(k, size):
    """Small templates match small bits of scenery by accident; make them pay for it."""
    return SCALE_PENALTY * max(0.0, float(np.log(PREF_SCALE * size / k)))


def _window_errors(q_lab, t_lab, alpha, y0, y1, x0, x1):
    """Mean-centred, truncated colour error for every placement of a k x k template
    with its top-left corner at rows [y0, y1) and columns [x0, x1) of the frame."""
    k = t_lab.shape[0]
    total = float(alpha.sum())
    if total < 1e-3 or y1 <= y0 or x1 <= x0:
        return None

    win = sliding_window_view(q_lab, (k, k), axis=(0, 1))[y0:y1, x0:x1]
    t_chw = np.moveaxis(t_lab, -1, 0)
    mean_t = (t_chw * alpha).sum(axis=(1, 2)) / total
    mean_q = np.einsum("yxckl,kl->yxc", win, alpha) / total

    diff = win - t_chw
    diff -= (mean_q - mean_t)[..., None, None]
    np.square(diff, out=diff)
    dist = diff.sum(axis=2)
    np.sqrt(dist, out=dist)
    np.minimum(dist, FIT_TRUNC, out=dist)
    return np.einsum("yxkl,kl->yx", dist, alpha) / total


def _coarse_search(q32, get_template, wide):
    best = None
    seen = set()
    for rel in SCALE_REL:
        k = max(8, min(COARSE, int(round(rel * COARSE))))
        if k in seen:
            continue
        seen.add(k)

        t_lab, alpha = get_template(k)
        limit = COARSE - k
        if wide:
            lo, hi = 0, limit
        else:
            radius = max(2, int(round(NARROW_RANGE * COARSE)))
            centre = limit / 2.0
            lo = max(0, int(np.floor(centre - radius)))
            hi = min(limit, int(np.ceil(centre + radius)))

        errs = _window_errors(q32, t_lab, alpha, lo, hi + 1, lo, hi + 1)
        if errs is None:
            continue
        iy, ix = np.unravel_index(int(errs.argmin()), errs.shape)
        err = float(errs[iy, ix]) + _scale_cost(k, COARSE)
        if best is None or err < best[0]:
            best = (err, k, lo + int(iy), lo + int(ix))
    return best


def _refine(q64, get_template, k_c, oy_c, ox_c, spread=3):
    centre_y = UPSCALE * oy_c + UPSCALE * k_c / 2.0
    centre_x = UPSCALE * ox_c + UPSCALE * k_c / 2.0
    best = None
    for delta in (-4, -2, 0, 2, 4):
        k = max(K_MIN, min(FRAME, UPSCALE * k_c + delta))
        t_lab, alpha = get_template(k)
        limit = FRAME - k
        ty = min(max(int(round(centre_y - k / 2.0)), 0), limit)
        tx = min(max(int(round(centre_x - k / 2.0)), 0), limit)
        y0, y1 = max(0, ty - spread), min(limit, ty + spread)
        x0, x1 = max(0, tx - spread), min(limit, tx + spread)

        errs = _window_errors(q64, t_lab, alpha, y0, y1 + 1, x0, x1 + 1)
        if errs is None:
            continue
        iy, ix = np.unravel_index(int(errs.argmin()), errs.shape)
        err = float(errs[iy, ix]) + _scale_cost(k, FRAME)
        if best is None or err < best[0]:
            best = (err, k, y0 + int(iy), x0 + int(ix))

    if best is None:
        k = max(K_MIN, min(FRAME, UPSCALE * k_c))
        best = (FIT_TRUNC, k, min(UPSCALE * oy_c, FRAME - k), min(UPSCALE * ox_c, FRAME - k))
    return best


class _TemplateCache:
    """Resized/Lab-converted templates, shared by every step of one verification."""

    def __init__(self, templates):
        self.templates = templates
        self.cache = {}

    def get(self, t, k):
        key = (t, k)
        if key not in self.cache:
            self.cache[key] = _template_at(self.templates[t], k)
        return self.cache[key]


def _fit_class(cache, idxs, q32, q64):
    """Best free placement of any template of one class -> (error, k, oy, ox, template)."""
    best = None
    for t in idxs:
        get = lambda k, t=t: cache.get(t, k)
        res = _coarse_search(q32, get, wide=False)
        if res is not None and res[0] > WIDE_TRIGGER:
            wide = _coarse_search(q32, get, wide=True)
            if wide is not None and wide[0] < res[0]:
                res = wide
        if res is not None and (best is None or res[0] < best[0]):
            best = res + (t,)

    if best is None:
        return None

    _, k_c, oy_c, ox_c, t = best
    err, k, oy, ox = _refine(q64, lambda k: cache.get(t, k), k_c, oy_c, ox_c)
    return err, k, oy, ox, t


def _local_placement(cache, groups, q64, prior):
    """Cheap re-alignment of every class around a placement that worked before;
    the best-fitting class decides."""
    k0, y0, x0 = prior
    centre_y, centre_x = y0 + k0 / 2.0, x0 + k0 / 2.0
    best = None
    for delta in (-2, -1, 0, 1, 2):
        k = max(K_MIN, min(FRAME, k0 + delta))
        limit = FRAME - k
        ty = min(max(int(round(centre_y - k / 2.0)), 0), limit)
        tx = min(max(int(round(centre_x - k / 2.0)), 0), limit)
        for idxs in groups:
            for t in idxs:
                t_lab, alpha = cache.get(t, k)
                errs = _window_errors(
                    q64, t_lab, alpha, max(0, ty - 1), min(limit, ty + 1) + 1,
                    max(0, tx - 1), min(limit, tx + 1) + 1,
                )
                if errs is None:
                    continue
                iy, ix = np.unravel_index(int(errs.argmin()), errs.shape)
                err = float(errs[iy, ix])
                if best is None or err < best[0]:
                    best = (err, k, max(0, ty - 1) + int(iy), max(0, tx - 1) + int(ix))
    return best


def _class_distance(q_lab, q_pad, t_lab, alpha, oy, ox, tolerant):
    """Per-pixel delta E between a placed template and the query (median-centred so a
    global tint cancels out, forgiving TOLERANCE pixels of misalignment)."""
    k = t_lab.shape[0]
    core = alpha > 0.5
    if int(core.sum()) < 8:
        return None

    shift = np.median((q_lab[oy : oy + k, ox : ox + k] - t_lab)[core], axis=0)
    if not tolerant:
        diff = q_lab[oy : oy + k, ox : ox + k] - shift - t_lab
        return np.sqrt((diff * diff).sum(axis=-1))

    span = 2 * TOLERANCE + 1
    best = None
    for dy in range(span):
        for dx in range(span):
            diff = q_pad[oy + dy : oy + dy + k, ox + dx : ox + dx + k] - shift - t_lab
            d2 = (diff * diff).sum(axis=-1)
            best = d2 if best is None else np.minimum(best, d2, out=best)
    return np.sqrt(best)


def _error_at(cache, idxs, q_lab, q_pad, k, oy, ox, tolerant):
    """Fit error of a class (its best template) at one fixed placement
    -> (error, template index, distance map or None)."""
    best = None
    for t in idxs:
        t_lab, alpha = cache.get(t, k)
        dist = _class_distance(q_lab, q_pad, t_lab, alpha, oy, ox, tolerant)
        if dist is None:
            err = FIT_TRUNC
        else:
            err = float((np.minimum(dist, FIT_TRUNC) * alpha).sum() / alpha.sum())
        if best is None or err < best[0]:
            best = (err, t, dist)
    return best


def _placement_contrast(cache, t, q_lab, q_pad, k, oy, ox, best_err):
    """How much worse the same template fits when nudged away from its best place
    (sideways and a little smaller/larger). A real match is sharply peaked, a template
    that merely resembles some scenery is not. None when too few probes fit the frame."""
    step = max(3, int(round(NULL_STEP * k)))
    probes = []
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
        probes.append((k, oy + dy * step, ox + dx * step))
    for dk in (-6, 6):
        k2 = k + dk
        probes.append((k2, int(round(oy + k / 2.0 - k2 / 2.0)), int(round(ox + k / 2.0 - k2 / 2.0))))

    errs = []
    for k2, y, x in probes:
        if k2 < K_MIN or k2 > FRAME or not (0 <= y <= FRAME - k2 and 0 <= x <= FRAME - k2):
            continue
        t_lab, alpha = cache.get(t, k2)
        dist = _class_distance(q_lab, q_pad, t_lab, alpha, y, x, False)
        if dist is not None:
            errs.append(float((np.minimum(dist, FIT_TRUNC) * alpha).sum() / alpha.sum()))
    if len(errs) < 3:
        return None
    return float(np.median(errs)) - best_err


def _box3(x):
    height, width = x.shape[:2]
    pad = np.pad(x, ((1, 1), (1, 1)) + ((0, 0),) * (x.ndim - 2), mode="edge")
    out = np.zeros_like(x)
    for dy in range(3):
        for dx in range(3):
            out += pad[dy : dy + height, dx : dx + width]
    return out / 9.0


def _pairwise_losses(cache, picks, k):
    """Head-to-head colour comparison of every pair of contenders.

    All templates sit at the same placement. For each pair the pixels are weighted by
    how much those two templates differ (a chest mark, a flower), and each
    contender's loss is how much worse than its rival it explains the query on those
    pixels. picks: [(template index, distance map)] -> one loss per contender."""
    alphas, blurred, dists = [], [], []
    for t, dist in picks:
        t_lab, alpha = cache.get(t, k)
        alphas.append(alpha)
        blurred.append(_box3(t_lab))
        dists.append(None if dist is None else np.minimum(dist, COL_TRUNC))

    losses = np.zeros(len(picks), dtype=np.float32)
    for a in range(len(picks)):
        for b in range(a + 1, len(picks)):
            if dists[a] is None or dists[b] is None:
                continue
            core = (alphas[a] > 0.6) & (alphas[b] > 0.6)
            if int(core.sum()) < 40:
                continue
            diff = blurred[a] - blurred[b]
            spread = np.sqrt((diff * diff).sum(axis=-1))
            weights = np.clip(spread - DIFF_DEADZONE, 0.0, None) * core
            total = float(weights.sum())
            if total < DIFF_MIN_WEIGHT:
                continue
            err_a = float((weights * dists[a]).sum() / total)
            err_b = float((weights * dists[b]).sum() / total)
            losses[a] = max(losses[a], err_a - err_b)
            losses[b] = max(losses[b], err_b - err_a)
    return losses


def verify_contenders(templates, tpl_index, frame, names, prior=None):
    """Returns ({class name: score adjustment <= 0}, debug info) or None when
    the templates cannot settle anything (missing template, poor alignment).

    prior: optional dict remembering the placement that worked before (the
    artwork sits in the same place in every spawn), which skips the full search."""
    _load_heavy()
    names = [name for name in names if tpl_index.get(name)]
    if len(names) < 2:
        return None
    groups = [tpl_index[name] for name in names]

    q64 = rgb_to_lab(frame)
    q_pad = np.pad(
        q64, ((TOLERANCE, TOLERANCE), (TOLERANCE, TOLERANCE), (0, 0)), mode="edge"
    )
    cache = _TemplateCache(templates)

    shared = None
    used_prior = False
    if prior is not None and prior.get("placement") is not None:
        local = _local_placement(cache, groups, q64, prior["placement"])
        if local is not None and local[0] <= FIT_GOOD:
            shared = local[1:]
            used_prior = True

    if shared is None:
        q32 = rgb_to_lab(
            frame.reshape(COARSE, UPSCALE, COARSE, UPSCALE, 3)
            .astype(np.float32)
            .mean(axis=(1, 3))
        )

        # 1. every class finds its own best placement
        own = []
        for idxs in groups:
            fit = _fit_class(cache, idxs, q32, q64)
            if fit is None:
                return None
            own.append(fit)

        # 2. the placement that suits most classes at once wins; a class that only
        #    matches somewhere odd and small cannot drag everybody there
        best_total = None
        for transform in {(f[1], f[2], f[3]) for f in own}:
            total = 0.0
            for idxs in groups:
                err, _, _ = _error_at(cache, idxs, q64, q_pad, *transform, False)
                total += min(err, FIT_TRUNC)
            if best_total is None or total < best_total:
                best_total, shared = total, transform

    k, oy, ox = shared

    # 3. every class measured at the shared placement
    at_shared = [_error_at(cache, idxs, q64, q_pad, k, oy, ox, True) for idxs in groups]
    fit_err = np.array([e for e, _, _ in at_shared], dtype=np.float32)
    fit_min = float(fit_err.min())

    trust = float(np.clip((FIT_BAD - fit_min) / (FIT_BAD - FIT_GOOD), 0.0, 1.0))
    best = int(fit_err.argmin())
    plain_best, _, _ = _error_at(cache, [at_shared[best][1]], q64, q_pad, k, oy, ox, False)
    contrast = _placement_contrast(cache, at_shared[best][1], q64, q_pad, k, oy, ox, plain_best)
    if contrast is None:
        trust *= 0.5
    else:
        trust *= float(np.clip((contrast - CONTRAST_LO) / (CONTRAST_HI - CONTRAST_LO), 0.0, 1.0))
    if trust <= 0.0:
        return None
    if prior is not None and not used_prior and fit_min <= FIT_GOOD and trust >= 0.9 and k >= K_MIN:
        prior["placement"] = shared

    # 4. head-to-head comparison on the pixels where each pair differs
    losses = _pairwise_losses(cache, [(t, d) for _, t, d in at_shared], k)

    adjust = {}
    for i, name in enumerate(names):
        penalty = BETA_FIT * max(0.0, float(fit_err[i]) - fit_min - FIT_DEADZONE)
        penalty += BETA_COL * float(losses[i])
        adjust[name] = -min(ADJ_CAP, trust * penalty)

    info = {
        "fit": {n: round(float(e), 2) for n, e in zip(names, fit_err)},
        "loss": {n: round(float(v), 2) for n, v in zip(names, losses)},
        "trust": round(trust, 2),
        "contrast": None if contrast is None else round(contrast, 2),
        "placement": shared,
        "prior": used_prior,
    }
    return adjust, info


def resolve_file_path(filename: str) -> Path:
    """Searches for a file in the cog folder first, then falls back to current working directory."""
    cog_dir_path = Path(__file__).resolve().parent / filename
    root_dir_path = Path.cwd() / filename

    if cog_dir_path.exists():
        return cog_dir_path
    if root_dir_path.exists():
        return root_dir_path

    raise FileNotFoundError(
        f"Could not locate '{filename}'. Checked:\n"
        f"  - {cog_dir_path}\n"
        f"  - {root_dir_path}"
    )


def trim_memory():
    """Hand freed heap pages back to the OS (matters on small hosts)."""
    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def format_name(name: str) -> str:
    """Capitalizes names correctly while preserving apostrophes, hyphens, colons, and percentages."""
    if not name:
        return ""

    words = name.split(" ")
    formatted_words = []

    for word in words:
        hyphen_parts = word.split("-")
        formatted_hyphen_parts = []

        for part in hyphen_parts:
            colon_parts = part.split(":")
            formatted_colon_parts = []

            for c_part in colon_parts:
                if "'" in c_part:
                    apo_parts = c_part.split("'")
                    formatted_colon_parts.append(
                        apo_parts[0].capitalize() + "'" + apo_parts[1].lower()
                    )
                else:
                    formatted_colon_parts.append(c_part.capitalize())

            formatted_hyphen_parts.append(":".join(formatted_colon_parts))

        formatted_words.append("-".join(formatted_hyphen_parts))

    formatted_str = " ".join(formatted_words)
    formatted_str = re.sub(r"\b(Jangmo|Hakamo|Kommo)-O\b", r"\1-o", formatted_str)
    return formatted_str


def extract_pokemon_from_text(text: str) -> str | None:
    """Extracts the Pokémon name from caught/fled messages."""
    cleaned_text = re.sub(r"<a?:[a-zA-Z0-9_]+:\d+>", "", text)
    cleaned_text = re.sub(r"[♂♀♂️♀️✨]", "", cleaned_text)

    caught_match = re.search(
        r"you caught a level \d+\s+([^\(!]+?)\s*(?:\(|\!)",
        cleaned_text,
        re.IGNORECASE,
    )
    if caught_match:
        return caught_match.group(1).strip().lower()

    fled_match = re.search(
        r"wild\s+([^\!\.\n]+?)\s+fled", cleaned_text, re.IGNORECASE
    )
    if fled_match:
        return fled_match.group(1).strip().lower()

    return None


class PokemonRecognizer:
    def __init__(self):
        self.ort_session = None
        self.input_name = None
        self.db_vectors = None
        self.db_names = None
        self.templates = None
        self.tpl_index = {}
        self.converts_sorted = []
        self.pokevars = set()
        self.pokeinfo = {}
        self._sims = None
        self._chunk = None
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._calls = 0
        self._prior = {}
        self._class_names = []
        self._order = None
        self._starts = None
        self._ends = None

    def load_resources(self):
        """Loads lightweight ONNX model & memory-mapped vector db from disk."""
        _load_heavy()
        import onnxruntime as ort

        # 1. Load ONNX Vision Model with low-memory settings
        try:
            model_path = resolve_file_path("models/clip_vision_quantized.onnx")
            opts = ort.SessionOptions()
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            opts.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            )
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False

            opts.add_session_config_entry("session.enable_cpu_mem_arena", "0")
            opts.add_session_config_entry(
                "session.use_device_allocator_for_initializers", "1"
            )

            self.ort_session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self.input_name = self.ort_session.get_inputs()[0].name
        except Exception as e:
            print(f"[Recognize] Warning: Could not load models/clip_vision_quantized.onnx ({e})")

        # 2. Memory-map vectors directly from disk without consuming RAM
        try:
            vectors_path = resolve_file_path("models/pokemon_db_vectors.npy")
            names_path = resolve_file_path("models/pokemon_db_names.json")

            self.db_vectors = np.load(str(vectors_path), mmap_mode="r")

            with open(names_path, "r", encoding="utf-8") as f:
                self.db_names = json.load(f)

            class_index = {}
            ids = np.fromiter(
                (class_index.setdefault(n, len(class_index)) for n in self.db_names),
                dtype=np.int32,
                count=len(self.db_names),
            )
            counts = np.bincount(ids, minlength=len(class_index))
            self._class_names = list(class_index)
            self._ends = np.cumsum(counts)
            self._starts = self._ends - counts
            self._order = np.argsort(ids, kind="stable").astype(np.int32)

            rows, dim = self.db_vectors.shape
            self._sims = np.empty(rows, dtype=np.float32)
            self._chunk = np.empty((min(SIM_CHUNK_ROWS, rows), dim), dtype=np.float32)
        except Exception as e:
            print(f"[Recognize] Warning: Could not memory-map vector db ({e})")

        # 2b. Optional class templates used to separate near-identical forms
        self.templates = None
        self.tpl_index = {}
        try:
            templates_path = resolve_file_path("models/pokemon_db_templates.npy")
            tpl_names_path = resolve_file_path("models/pokemon_db_template_names.json")

            templates = np.load(str(templates_path), mmap_mode="r")
            with open(tpl_names_path, "r", encoding="utf-8") as f:
                tpl_names = json.load(f)

            if (
                templates.ndim == 4
                and templates.shape[3] == 4
                and templates.shape[1] == templates.shape[2]
                and templates.shape[0] == len(tpl_names)
            ):
                self.templates = templates
                for i, name in enumerate(tpl_names):
                    self.tpl_index.setdefault(name, []).append(i)
            else:
                print(
                    "[Recognize] Warning: template files do not match each other, "
                    "pixel verification disabled. Re-run indexer.py."
                )
        except Exception:
            print(
                "[Recognize] Note: models/pokemon_db_templates.npy not found, "
                "pixel verification disabled. Re-run indexer.py to enable it."
            )

        # 3. Load constant local datasets
        try:
            converts_path = resolve_file_path("data/pokes/converts.json")
            with open(converts_path, "r", encoding="utf-8") as f:
                converts = json.load(f)
                self.converts_sorted = [
                    (key, key.lower(), val)
                    for key, val in sorted(
                        converts.items(), key=lambda x: len(x[0]), reverse=True
                    )
                ]
        except Exception:
            self.converts_sorted = []

        try:
            pokevars_path = resolve_file_path("data/pokevars.json")
            with open(pokevars_path, "r", encoding="utf-8") as f:
                pokevars_list = json.load(f)
                self.pokevars = {name.strip().lower() for name in pokevars_list}
        except Exception:
            self.pokevars = set()

        try:
            pokeinfo_path = resolve_file_path("pokeinfo.json")
            with open(pokeinfo_path, "r", encoding="utf-8") as f:
                pokeinfo_raw = json.load(f)
                self.pokeinfo = {
                    k.strip().lower(): {
                        "types": v.get("types", []) if isinstance(v, dict) else [],
                        "region": v.get("region", "") if isinstance(v, dict) else ""
                    }
                    for k, v in pokeinfo_raw.items()
                }
        except Exception:
            self.pokeinfo = {}

        trim_memory()

    def apply_conversions(self, name: str) -> str:
        if not self.converts_sorted:
            return name

        current = name
        current_lower = name.lower()
        for key, key_lower, val in self.converts_sorted:
            if key_lower in current_lower:
                current = re.sub(re.escape(key), val, current, flags=re.IGNORECASE)
                current_lower = current.lower()

        return current

    def _similarities(self, target):
        """Cosine similarities in float32, computed in chunks over the mmap'd db
        into reusable buffers (no per-query allocations)."""
        total = self.db_vectors.shape[0]
        sims = self._sims

        for start in range(0, total, SIM_CHUNK_ROWS):
            end = min(start + SIM_CHUNK_ROWS, total)
            rows = end - start
            chunk = self._chunk[:rows]
            chunk[...] = self.db_vectors[start:end]
            np.dot(chunk, target, out=sims[start:end])

        return sims

    def _verify(self, frame, contenders):
        if self.templates is None or frame is None:
            return None
        try:
            result = verify_contenders(
                self.templates, self.tpl_index, frame, [c[1] for c in contenders], self._prior
            )
        except Exception as e:
            print(f"[Recognize] Template verification skipped ({e})")
            return None

        if result is None:
            return None
        adjust, info = result
        if DEBUG:
            print(f"[Recognize] verify {info} adjust={ {k: round(v, 3) for k, v in adjust.items()} }")
        return adjust

    def _extra_contenders(self, sims, known, floor):
        """Classes none of whose images made the top-K list but whose best image is
        still within the re-rank margin (e.g. a near-identical sibling that has fewer
        images than the class dominating the list)."""
        if self._order is None:
            return []

        class_best = np.maximum.reduceat(sims[self._order], self._starts)
        extras = []
        for c in np.flatnonzero(class_best >= floor):
            name = self._class_names[int(c)]
            if name in known:
                continue
            rows = self._order[self._starts[c] : self._ends[c]]
            vals = sims[rows]
            top = np.argsort(-vals)[:CLASS_TOP_N]
            score = CLASS_MAX_WEIGHT * float(vals[top[0]]) + (1.0 - CLASS_MAX_WEIGHT) * float(vals[top].mean())
            extras.append((score, name, [int(rows[top[0]])]))
        return extras

    def _rank(self, sims, frame=None) -> tuple[str, float]:
        """Class-level scoring over the top images, then pixel verification of close contenders."""
        total = sims.shape[0]
        k = min(TOP_K_IMAGES, total)

        if k < total:
            top = np.argpartition(-sims, k - 1)[:k]
        else:
            top = np.arange(total)
        top = top[np.argsort(-sims[top])]

        groups = {}
        for idx in top:
            groups.setdefault(self.db_names[int(idx)], []).append(int(idx))

        scored = []
        for name, idxs in groups.items():
            vals = sims[idxs[:CLASS_TOP_N]]
            score = CLASS_MAX_WEIGHT * float(vals[0]) + (1.0 - CLASS_MAX_WEIGHT) * float(vals.mean())
            scored.append((score, name, idxs))
        scored.sort(key=lambda item: -item[0])

        winner = scored[0]

        if self.templates is not None:
            best_score = scored[0][0]
            contenders = [
                item
                for item in scored[:RERANK_MAX_CLASSES]
                if best_score - item[0] <= RERANK_MARGIN
            ]
            contenders += self._extra_contenders(
                sims, {item[1] for item in scored}, best_score - RERANK_MARGIN
            )
            contenders.sort(key=lambda item: -item[0])
            contenders = contenders[:RERANK_MAX_CLASSES]

            if len(contenders) > 1:
                adjust = self._verify(frame, contenders)
                if adjust:
                    adjusted = [
                        (score + adjust.get(name, 0.0), name, idxs)
                        for score, name, idxs in contenders
                    ]
                    adjusted.sort(key=lambda item: -item[0])
                    winner = adjusted[0]

        _, name, idxs = winner
        return str(name), float(sims[idxs[0]])

    def predict_from_image_bytes(self, image_bytes: bytes) -> tuple[str, float]:
        if self.ort_session is None or self.db_vectors is None:
            raise RuntimeError("ONNX Session or Database not loaded properly.")

        _load_heavy()
        key = hashlib.blake2b(image_bytes, digest_size=16).digest()

        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit

            with Image.open(io.BytesIO(image_bytes)) as img:
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image dimensions are too large.")
                pixels, frame = preprocess_image(img)

            outputs = self.ort_session.run(None, {self.input_name: pixels[None, ...]})

            target_vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(target_vector))
            if norm != 0:
                target_vector = target_vector / norm

            similarities = self._similarities(target_vector)
            raw_name, confidence = self._rank(similarities, frame)
            result = (self.apply_conversions(raw_name), confidence)

            self._cache[key] = result
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)

            self._calls += 1
            if self._calls % TRIM_EVERY == 0:
                trim_memory()

            return result

    async def identify_from_url(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[str, float]:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download image. HTTP {resp.status}")
            if resp.content_length and resp.content_length > MAX_IMAGE_BYTES:
                raise ValueError("Image is too large.")

            chunks = []
            received = 0
            async for chunk in resp.content.iter_chunked(65536):
                received += len(chunk)
                if received > MAX_IMAGE_BYTES:
                    raise ValueError("Image is too large.")
                chunks.append(chunk)
            image_bytes = b"".join(chunks)

        return await asyncio.to_thread(
            self.predict_from_image_bytes, image_bytes
        )