"""Compare subject crops with an installed DINOv2 checkpoint, offline on the CPU."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path

from .. import proc

METHOD = "dinov2-cosine-v1"


def prepare(image):
    from PIL import Image

    image = image.convert("RGB").resize((224, 224), Image.Resampling.BICUBIC)
    low, high = image.convert("L").getextrema()
    if high - low < 8:
        return None
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", compress_level=1)
    return base64.b64encode(encoded.getvalue()).decode("ascii")


def compare(shots):
    model = os.environ.get("HOARD_HUB_IDENTITY_MODEL")
    if not model:
        raise RuntimeError("Set HOARD_HUB_IDENTITY_MODEL to the installed DINOv2 model directory")
    python = os.environ.get("HOARD_HUB_IDENTITY_PYTHON") or sys.executable
    env = proc.build_env(CUDA_VISIBLE_DEVICES="-1", HF_HUB_OFFLINE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    result = proc.run([python, "-m", __name__, str(Path(model).expanduser().resolve())],
                      cwd=Path(__file__).resolve().parents[2], env=env, input=json.dumps(shots), timeout=75)
    if result.returncode:
        raise RuntimeError(proc.tail_lines(result.stderr))
    return json.loads(result.stdout)


def _scores(shots, vectors):
    scores = []
    for shot in shots:
        references = [(index, vectors[crop]) for index, crop in enumerate(shot["references"], 1) if crop in vectors]
        rows = []
        for crop in shot["samples"]:
            if crop not in vectors or not references:
                rows.append({"similarity": None})
                continue
            ref, score = max(((index, float(vectors[crop] @ vector)) for index, vector in references),
                             key=lambda item: item[1])
            rows.append({"similarity": round(max(0.0, min(1.0, score)), 5), "reference": ref})
        scores.append(rows)
    return scores


def _measure(model_dir, shots):
    import numpy as np
    import torch
    from PIL import Image
    from transformers import Dinov2Model

    torch.set_num_threads(4)
    weights = model_dir / "model.safetensors"
    with weights.open("rb") as file:
        sha = hashlib.file_digest(file, "sha256").hexdigest()
    model = Dinov2Model.from_pretrained(model_dir, local_files_only=True, use_safetensors=True).to("cpu").eval()
    mean = torch.tensor([.485, .456, .406])
    std = torch.tensor([.229, .224, .225])
    crops = list(dict.fromkeys(crop for shot in shots for part in ("references", "samples")
                              for crop in shot[part] if crop is not None))
    vectors = {}
    with torch.inference_mode():
        for start in range(0, len(crops), 8):
            batch = crops[start:start + 8]
            images = []
            for crop in batch:
                with Image.open(io.BytesIO(base64.b64decode(crop))) as image:
                    images.append(np.asarray(image))
            pixels = (torch.from_numpy(np.stack(images)).float() / 255 - mean) / std
            embedding = model(pixel_values=pixels.permute(0, 3, 1, 2)).pooler_output
            vectors.update(zip(batch, torch.nn.functional.normalize(embedding).numpy()))
    return {"model": {"type": model.config.model_type, "weights_sha256": sha}, "scores": _scores(shots, vectors)}


if __name__ == "__main__":
    print(json.dumps(_measure(Path(sys.argv[1]), json.load(sys.stdin)), allow_nan=False))
