from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .config import Config


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Falcon MCP local control + on-demand timbre analysis")
    parser.add_argument(
        "command",
        choices=["serve", "prepare", "setup", "download-model", "describe", "status"],
        default="serve",
        nargs="?",
    )
    parser.add_argument("path", nargs="?", help="Audio path for describe")
    parser.add_argument("--verify-watch", action="store_true", help="Test automatic script reload when idle")
    args = parser.parse_args()
    cfg = Config.from_env()
    if args.command == "serve":
        from .server import create_server

        create_server(cfg).run(transport="stdio")
    elif args.command == "prepare":
        from .bridge import FalconBridge

        print(json.dumps(FalconBridge(cfg).prepare(), ensure_ascii=False, indent=2))
    elif args.command == "setup":
        from .bridge import FalconBridge
        from .setup import setup_bridge

        result = asyncio.run(setup_bridge(FalconBridge(cfg), args.verify_watch))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "download-model":
        # Download only. Do not load weights into Torch or allocate GPU memory.
        from huggingface_hub import snapshot_download

        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        cfg.model_cache.mkdir(parents=True, exist_ok=True)
        path = snapshot_download(
            cfg.model_id,
            cache_dir=str(cfg.model_cache),
            allow_patterns=[
                "*.json",
                "*.txt",
                "*.model",
                "*.safetensors",
                "pytorch_model.bin",
                "merges.txt",
                "vocab.json",
                "README.md",
                "LICENSE*",
            ],
        )
        print(json.dumps({"downloaded": path, "model_loaded": False}, ensure_ascii=False))
    elif args.command == "describe":
        from .audio import FeedbackRunner

        if not args.path:
            parser.error("describe requires an audio path under FALCON_AUDIO_ROOT")
        result = asyncio.run(FeedbackRunner(cfg).analyze(cfg.audio_path(args.path)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "status":
        from .audio import FeedbackRunner
        from .bridge import FalconBridge

        async def status():
            result = {"feedback": FeedbackRunner(cfg).status()}
            try:
                result["engine"] = await FalconBridge(cfg).request("ping")
            except (RuntimeError, TimeoutError, OSError) as exc:
                result["error"] = str(exc)
            return result

        print(json.dumps(asyncio.run(status()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
