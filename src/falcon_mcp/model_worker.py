"""One-shot local inference. Heavy ML imports exist ONLY inside main()."""

from __future__ import annotations

import contextlib
import json
import sys
from importlib.resources import files
from pathlib import Path


def infer(request: dict) -> dict:
    import numpy as np
    import torch
    from scipy.signal import resample_poly
    from torch.nn import functional
    from transformers import ClapModel, ClapProcessor

    from .audio import read_audio

    # Avoid surprising CPU thread oversubscription in a one-shot audio worker.
    torch.set_num_threads(min(4, torch.get_num_threads()))
    device = request["device"]
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device not in {"cpu", "cuda"}:
        raise ValueError("FALCON_MODEL_DEVICE must be cpu, cuda or auto")
    model_options = {"cache_dir": request["cache"], "local_files_only": request["offline"]}
    processor = ClapProcessor.from_pretrained(request["model_id"], **model_options)
    # The upstream LAION checkpoint uses pytorch_model.bin; Torch >=2.6 uses
    # weights_only loading. Prefer safetensors automatically when supplied locally.
    model = ClapModel.from_pretrained(request["model_id"], **model_options).to(device).eval()
    bank = json.loads(files("falcon_mcp").joinpath("data/timbre_descriptors.json").read_text(encoding="utf8"))
    descriptors = [item for group in bank for item in group["items"]]
    targets = request.get("targets", [])
    texts = [item["text"] for item in descriptors] + targets
    audio, rate = read_audio(Path(request["path"]))
    # Preserve audible signal when stereo happens to be phase-inverted.
    mono = audio.mean(axis=1)
    if np.sqrt(np.mean(mono**2)) < np.sqrt(np.mean(audio**2)) * 0.1:
        mono = audio[:, int(np.argmax(np.mean(audio**2, axis=0)))]
    if rate != 48000:
        import math

        divisor = math.gcd(rate, 48000)
        mono = resample_poly(mono, 48000 // divisor, rate // divisor).astype(np.float32)
    # Every window contributes. Never let the processor randomly crop a long clip.
    windows = [mono[i : i + 480000] for i in range(0, len(mono), 480000)]
    weights = [len(window) for window in windows]
    with torch.inference_mode():
        text_inputs = processor(text=texts, return_tensors="pt", padding=True)
        text_inputs = {key: value.to(device) for key, value in text_inputs.items()}
        text_features = model.get_text_features(**text_inputs)
        # Transformers 4.x returns Tensor; accept named model outputs from later APIs.
        if not isinstance(text_features, torch.Tensor):
            text_features = text_features.pooler_output
        text_features = functional.normalize(text_features, dim=-1)
        all_scores = []
        for window in windows:
            # Deterministic right-padding for a short final window.
            padded = np.pad(window, (0, max(0, 480000 - len(window))))
            # Calling the feature extractor directly works across 4.x releases whose
            # processor renamed `audios` to `audio`.
            inputs = processor.feature_extractor(padded, sampling_rate=48000, return_tensors="pt")
            inputs = {key: value.to(device) for key, value in inputs.items()}
            features = model.get_audio_features(**inputs)
            if not isinstance(features, torch.Tensor):
                features = features.pooler_output
            features = functional.normalize(features, dim=-1)
            all_scores.append((features @ text_features.T)[0].float().cpu().numpy())
        scores = np.average(all_scores, axis=0, weights=weights)
    facets, cursor = [], 0
    for group in bank:
        length = len(group["items"])
        indices = np.argsort(scores[cursor : cursor + length])[::-1]
        ranked = [
            {**group["items"][int(i)], "cosine_similarity": float(scores[cursor + int(i)])}
            for i in indices[:3]
        ]
        margin = ranked[0]["cosine_similarity"] - ranked[1]["cosine_similarity"]
        facets.append(
            {"facet": group["facet"], "top_matches": ranked, "margin": margin, "ambiguous": margin < 0.02}
        )
        cursor += length
    summary = "; ".join(
        f"{facet['facet']}: {facet['top_matches'][0]['label']}"
        + (" (close matches)" if facet["ambiguous"] else "")
        for facet in facets
    )
    target_scores = [
        {"text": text, "cosine_similarity": float(scores[cursor + i])} for i, text in enumerate(targets)
    ]
    return {
        "description": summary + ".",
        "method": "CLAP audio/text descriptor retrieval",
        "model_id": request["model_id"],
        "device": device,
        "facets": facets,
        "target_matches": target_scores,
        "windows_analyzed": len(windows),
        "limitations": [
            "Similarity is relative, not a calibrated probability or a free-form caption.",
            "Descriptors depend on the candidate bank. Use identical notes, velocity and recording conditions for A/B.",
            "The model does not infer Falcon parameter values or automatically apply changes.",
        ],
    }


def main():
    request = json.loads(sys.stdin.buffer.read(65537))
    # Libraries sometimes print progress to stdout; preserve stdout for a single JSON response.
    with contextlib.redirect_stdout(sys.stderr):
        result = infer(request)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8"))


if __name__ == "__main__":
    main()
