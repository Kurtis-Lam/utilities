#!/usr/bin/env python3
"""Train a Pokémon image classifier and export a PyTorch-free ONNX model.

Expected data layout (one directory per exact output label):

    recognizer/traindata/
        abra/1.png ... 12.png
        finizen/1.png ... 12.png
        palafin/1.png ... 12.png
        flabebe-red-flower/1.png ... 12.png
        flabebe-blue-flower/1.png ... 12.png
        arceus-fire/1.png ... 12.png
        silvally-fire/1.png ... 12.png

Training requires PyTorch/torchvision. The exported model is run by recognize.py
through ONNX Runtime; PyTorch is not imported by the prediction module.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_IMAGE_SIZE = 224
BACKGROUND = (248, 248, 248)
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
ARCHITECTURE = "mobilenet_v3_large"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fine-tune MobileNetV3-Large and export an ONNX Pokémon classifier."
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Dataset root. Defaults to recognizer/traindata or traindata.",
    )
    parser.add_argument(
        "--output-dir",
        default="models/pokemon_mnv3_large",
        help="Output directory for model.onnx and metadata (default: models/pokemon_mnv3_large).",
    )
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--image-size", type=int, default=DEFAULT_IMAGE_SIZE)
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="DataLoader worker processes (default: 0 for reliable Windows/CPU training; try 2 on a fast local SSD).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--patience", type=int, default=9)
    parser.add_argument("--freeze-epochs", type=int, default=2)
    parser.add_argument("--backbone-lr", type=float, default=2e-5)
    parser.add_argument("--head-lr", type=float, default=4e-4)
    parser.add_argument(
        "--weights",
        choices=("DEFAULT", "none"),
        default="DEFAULT",
        help="Use ImageNet pretrained weights (recommended) or train from scratch.",
    )
    parser.add_argument(
        "--label-map",
        default=None,
        help="Optional JSON mapping directory names to exact prediction labels.",
    )
    parser.add_argument(
        "--keep-checkpoint",
        action="store_true",
        help="Keep best_model.pth in the output directory (not needed for inference).",
    )
    return parser.parse_args()


def locate_data_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Dataset directory does not exist: {path}")
        return path

    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / "recognizer" / "traindata",
        Path.cwd() / "traindata",
        here / "recognizer" / "traindata",
        here / "traindata",
        here.parent / "recognizer" / "traindata",
        here.parent / "traindata",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()

    tried = "\n".join(f"  - {p}" for p in candidates)
    raise FileNotFoundError(
        "Could not locate the training images. Pass --data-dir explicitly. Tried:\n" + tried
    )


def load_label_map(path_text: str | None) -> dict[str, str]:
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Label map not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("--label-map must point to a JSON object: {\"folder name\": \"label\"}")
    return {str(key): str(value).strip() for key, value in raw.items()}


def discover_samples(data_dir: Path, label_map: dict[str, str]) -> tuple[list[tuple[Path, int]], list[str], dict[str, int]]:
    class_dirs = sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not class_dirs:
        raise RuntimeError(f"No class directories found inside {data_dir}")

    labels_to_paths: dict[str, list[Path]] = {}
    folder_to_label: dict[str, str] = {}
    for class_dir in class_dirs:
        label = label_map.get(class_dir.name, class_dir.name).strip()
        if not label:
            raise ValueError(f"Empty label for folder {class_dir.name!r}")
        folder_to_label[class_dir.name] = label
        files = sorted(
            p for p in class_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
        if files:
            labels_to_paths.setdefault(label, []).extend(files)

    duplicated_labels = [label for label, paths in labels_to_paths.items() if len(paths) == 0]
    if duplicated_labels:
        raise RuntimeError(f"No images found for labels: {duplicated_labels}")

    labels = sorted(labels_to_paths, key=str.casefold)
    if len(labels) < 2:
        raise RuntimeError("At least two distinct class folders are required to train a classifier.")

    samples: list[tuple[Path, int]] = []
    counts: dict[str, int] = {}
    for class_index, label in enumerate(labels):
        paths = labels_to_paths[label]
        counts[label] = len(paths)
        if len(paths) == 1:
            print(f"[WARNING] {label!r} has only 1 image; it cannot have a validation sample.")
        elif len(paths) < 5:
            print(f"[WARNING] {label!r} has only {len(paths)} images; 12 per class is preferable.")
        samples.extend((path, class_index) for path in paths)

    # If a label-map accidentally maps multiple folders to one label, fail rather than
    # silently merging classes that may represent visually different forms.
    labels_seen = set(labels)
    mapped_folders: dict[str, list[str]] = {}
    for folder, label in folder_to_label.items():
        if any(p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS for p in (data_dir / folder).rglob("*")):
            mapped_folders.setdefault(label, []).append(folder)
    merged = {label: folders for label, folders in mapped_folders.items() if len(folders) > 1}
    if merged:
        raise ValueError(
            "Multiple folders map to the same output label. Keep forms separate, or fix --label-map: "
            + repr(merged)
        )

    return samples, labels, counts


def split_samples(
    samples: list[tuple[Path, int]], labels: list[str], seed: int
) -> tuple[list[tuple[Path, int]], list[tuple[Path, int]]]:
    rng = random.Random(seed)
    grouped: dict[int, list[tuple[Path, int]]] = {i: [] for i in range(len(labels))}
    for sample in samples:
        grouped[sample[1]].append(sample)

    train_samples: list[tuple[Path, int]] = []
    validation_samples: list[tuple[Path, int]] = []
    for class_index, items in grouped.items():
        rng.shuffle(items)
        if len(items) >= 5:
            validation_count = max(1, round(len(items) * 0.18))
        elif len(items) >= 2:
            validation_count = 1
        else:
            validation_count = 0
        validation_samples.extend(items[:validation_count])
        train_samples.extend(items[validation_count:])

    rng.shuffle(train_samples)
    rng.shuffle(validation_samples)
    return train_samples, validation_samples


def prepare_image(image: Any, image_size: int) -> Any:
    """Composite transparency onto a neutral canvas, preserve aspect ratio, and pad square."""
    from PIL import Image

    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (*BACKGROUND, 255))
    rgb = Image.alpha_composite(background, rgba).convert("RGB")
    rgb.thumbnail((image_size, image_size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (image_size, image_size), BACKGROUND)
    x = (image_size - rgb.width) // 2
    y = (image_size - rgb.height) // 2
    canvas.paste(rgb, (x, y))
    return canvas


class PokemonDataset:
    """Picklable map-style dataset used by PyTorch's DataLoader.

    This class intentionally lives at module scope. On Windows, DataLoader workers
    use the spawn multiprocessing method and must pickle the dataset. Defining the
    class inside a factory/``__new__`` method causes the exact
    ``Can't get local object`` failure seen when ``num_workers`` is greater than 0.

    It does not inherit from ``torch.utils.data.Dataset`` so importing this module
    for ``--help`` does not require importing PyTorch. DataLoader accepts this
    standard ``__len__``/``__getitem__`` interface.
    """

    def __init__(self, samples: list[tuple[Path, int]], image_size: int, training: bool) -> None:
        from torchvision import transforms

        self.samples = samples
        self.image_size = image_size
        if training:
            self.transform = transforms.Compose([
                transforms.RandomAffine(
                    degrees=9,
                    translate=(0.035, 0.035),
                    scale=(0.93, 1.07),
                    fill=BACKGROUND,
                ),
                transforms.RandomHorizontalFlip(p=0.5),
                # Deliberately no hue shift: flower/Arceus/Silvally colours are labels.
                transforms.ColorJitter(brightness=0.10, contrast=0.10, saturation=0.07),
                transforms.ToTensor(),
                transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
            ])
        else:
            self.transform = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
            ])

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[Any, int]:
        from PIL import Image

        path, target = self.samples[index]
        try:
            with Image.open(path) as image:
                image = prepare_image(image, self.image_size)
        except Exception as exc:
            raise RuntimeError(f"Could not read training image {path}: {exc}") from exc
        return self.transform(image), target


def make_model(num_classes: int, pretrained: bool):
    import torch.nn as nn
    from torchvision.models import MobileNet_V3_Large_Weights, mobilenet_v3_large

    weights = MobileNet_V3_Large_Weights.DEFAULT if pretrained else None
    model = mobilenet_v3_large(weights=weights)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, num_classes)
    return model


def evaluate(model: Any, loader: Any, criterion: Any, device: Any, torch: Any) -> tuple[float, float]:
    model.eval()
    total_loss = 0.0
    total_correct = 0
    total_items = 0
    with torch.inference_mode():
        for images, targets in loader:
            images = images.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            logits = model(images)
            loss = criterion(logits, targets)
            total_loss += float(loss.item()) * targets.size(0)
            total_correct += int((logits.argmax(dim=1) == targets).sum().item())
            total_items += int(targets.size(0))
    if total_items == 0:
        return math.inf, 0.0
    return total_loss / total_items, total_correct / total_items


def export_onnx(model: Any, output_path: Path, image_size: int, device: Any, torch: Any) -> None:
    model.eval()
    dummy = torch.zeros(1, 3, image_size, image_size, device=device)
    try:
        torch.onnx.export(
            model,
            dummy,
            str(output_path),
            input_names=["images"],
            output_names=["logits"],
            opset_version=17,
            do_constant_folding=True,
            dynamo=False,
        )
    except TypeError:
        # Compatibility with older PyTorch releases which do not accept dynamo=.
        torch.onnx.export(
            model,
            dummy,
            str(output_path),
            input_names=["images"],
            output_names=["logits"],
            opset_version=17,
            do_constant_folding=True,
        )

    try:
        import onnx
        onnx.checker.check_model(str(output_path))
    except ImportError:
        print("[WARNING] 'onnx' package not installed; skipped ONNX graph validation.")


def main() -> int:
    args = parse_args()
    if args.epochs < 1 or args.batch_size < 1 or args.image_size < 64:
        raise ValueError("epochs, batch-size, and image-size must be positive (image-size >= 64).")

    try:
        import numpy as np
        import torch
        import torch.nn as nn
        from torch.utils.data import DataLoader
    except ImportError as exc:
        raise SystemExit(
            "Training dependencies are missing. Install them in the training environment:\n"
            "  pip install torch torchvision onnx pillow numpy"
        ) from exc

    data_dir = locate_data_dir(args.data_dir)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    label_map = load_label_map(args.label_map)
    samples, labels, original_counts = discover_samples(data_dir, label_map)
    train_samples, validation_samples = split_samples(samples, labels, args.seed)
    if not train_samples:
        raise RuntimeError("No training images remain after splitting.")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cudnn.benchmark = True

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Dataset: {data_dir}")
    print(f"Classes: {len(labels)} | Images: {len(samples)} | Train: {len(train_samples)} | Validation: {len(validation_samples)}")
    print(f"Device: {device} | Architecture: {ARCHITECTURE} | Input: {args.image_size}x{args.image_size}")
    print("Every directory is treated as a distinct class; similarly named forms are never merged automatically.")

    worker_count = max(0, args.workers)
    if worker_count != args.workers:
        print(f"[WARNING] Negative --workers value {args.workers} was changed to 0.")

    train_dataset = PokemonDataset(train_samples, args.image_size, training=True)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=worker_count,
        pin_memory=(device.type == "cuda"),
        persistent_workers=(worker_count > 0),
    )

    validation_loader = None
    if validation_samples:
        validation_dataset = PokemonDataset(validation_samples, args.image_size, training=False)
        validation_loader = DataLoader(
            validation_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=worker_count,
            pin_memory=(device.type == "cuda"),
            persistent_workers=(worker_count > 0),
        )
    else:
        print("[WARNING] No validation images; the script will save the final training checkpoint.")

    model = make_model(len(labels), pretrained=(args.weights == "DEFAULT")).to(device)
    for parameter in model.features.parameters():
        parameter.requires_grad = False

    optimizer = torch.optim.AdamW(
        [
            {"params": model.features.parameters(), "lr": args.backbone_lr},
            {"params": model.classifier.parameters(), "lr": args.head_lr},
        ],
        weight_decay=1e-4,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))

    train_counts = Counter(class_index for _, class_index in train_samples)
    class_weights = torch.tensor(
        [len(train_samples) / max(1, len(labels) * train_counts[i]) for i in range(len(labels))],
        dtype=torch.float32,
        device=device,
    )
    criterion = nn.CrossEntropyLoss(weight=class_weights, label_smoothing=0.04)
    use_amp = device.type == "cuda"
    try:
        scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    except (AttributeError, TypeError):
        # Compatibility with older PyTorch releases.
        scaler = torch.cuda.amp.GradScaler(enabled=use_amp)

    best_state = None
    best_metric = -1.0
    best_loss = math.inf
    best_epoch = 0
    epochs_without_improvement = 0
    started = time.time()

    freeze_until = min(max(0, args.freeze_epochs), max(0, args.epochs - 1))
    for epoch in range(1, args.epochs + 1):
        if epoch == freeze_until + 1:
            for parameter in model.features.parameters():
                parameter.requires_grad = True
            print("Unfroze MobileNetV3 feature extractor for fine-tuning.")

        model.train()
        running_loss = 0.0
        processed = 0
        for images, targets in train_loader:
            images = images.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)

            if use_amp:
                try:
                    autocast_context = torch.amp.autocast("cuda")
                except (AttributeError, TypeError):
                    autocast_context = torch.cuda.amp.autocast()
                with autocast_context:
                    logits = model(images)
                    loss = criterion(logits, targets)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                logits = model(images)
                loss = criterion(logits, targets)
                loss.backward()
                optimizer.step()

            running_loss += float(loss.item()) * targets.size(0)
            processed += int(targets.size(0))

        scheduler.step()
        train_loss = running_loss / max(1, processed)

        if validation_loader is not None:
            val_loss, val_accuracy = evaluate(model, validation_loader, criterion, device, torch)
            metric = val_accuracy
            improved = (metric > best_metric) or (metric == best_metric and val_loss < best_loss)
            print(
                f"Epoch {epoch:02d}/{args.epochs} | train loss {train_loss:.4f} | "
                f"val loss {val_loss:.4f} | val accuracy {val_accuracy:.2%}"
            )
        else:
            val_loss, val_accuracy = None, None
            metric = -train_loss
            improved = metric > best_metric
            print(f"Epoch {epoch:02d}/{args.epochs} | train loss {train_loss:.4f}")

        if improved:
            best_metric = metric
            best_loss = val_loss if val_loss is not None else train_loss
            best_epoch = epoch
            best_state = copy.deepcopy(model.state_dict())
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1

        if validation_loader is not None and epochs_without_improvement >= args.patience:
            print(f"Early stopping at epoch {epoch}; best epoch was {best_epoch}.")
            break

    if best_state is None:
        raise RuntimeError("Training did not produce a model checkpoint.")

    model.load_state_dict(best_state)
    model.to(device).eval()

    onnx_path = output_dir / "model.onnx"
    export_onnx(model, onnx_path, args.image_size, device, torch)

    labels_path = output_dir / "labels.json"
    with labels_path.open("w", encoding="utf-8") as handle:
        json.dump({"labels": labels}, handle, ensure_ascii=False, indent=2)

    config = {
        "architecture": ARCHITECTURE,
        "image_size": args.image_size,
        "mean": list(IMAGENET_MEAN),
        "std": list(IMAGENET_STD),
        "background": list(BACKGROUND),
        "input_name": "images",
        "output_name": "logits",
        "class_count": len(labels),
        "model_file": "model.onnx",
        "labels_file": "labels.json",
        "training_framework": "PyTorch (training/export only)",
        "inference_runtime": "ONNX Runtime",
    }
    with (output_dir / "config.json").open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)

    report = {
        "dataset": str(data_dir),
        "class_count": len(labels),
        "image_count": len(samples),
        "train_image_count": len(train_samples),
        "validation_image_count": len(validation_samples),
        "images_per_class": original_counts,
        "best_epoch": best_epoch,
        "validation_accuracy": best_metric if validation_loader is not None else None,
        "validation_loss": best_loss if validation_loader is not None else None,
        "training_seconds": round(time.time() - started, 2),
        "model_file_size_mb": round(onnx_path.stat().st_size / (1024 * 1024), 2),
    }
    with (output_dir / "training_report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    if args.keep_checkpoint:
        torch.save({"model_state_dict": best_state, "labels": labels, "config": config}, output_dir / "best_model.pth")

    print("\nExport complete:")
    print(f"  ONNX model: {onnx_path}")
    print(f"  Labels:     {labels_path}")
    print(f"  Metadata:   {output_dir / 'config.json'}")
    print(f"  Report:     {output_dir / 'training_report.json'}")
    print("Copy the whole output directory to the bot host. Do not copy best_model.pth unless you need it for future training.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Training interrupted.", file=sys.stderr)
        raise SystemExit(130)
