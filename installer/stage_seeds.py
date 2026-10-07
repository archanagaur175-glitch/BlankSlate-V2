import json
import shutil
import sys
from pathlib import Path

SRC_OLLAMA = Path(r"C:\Users\archa\.ollama\models")
SRC_HF = Path(r"C:\Users\archa\.cache\huggingface\hub")
DST = Path(r"C:\Users\archa\Documents\BlankSlate_V2\installer\staging")

OLLAMA_MANIFESTS = [
    r"manifests\registry.ollama.ai\library\qwen2.5\0.5b",
    r"manifests\registry.ollama.ai\library\moondream\latest",
    r"manifests\registry.ollama.ai\library\nomic-embed-text\latest",
]
HF_REPOS = [
    "models--Systran--faster-whisper-small.en",
    "models--kyutai--pocket-tts",
    "models--kyutai--pocket-tts-without-voice-cloning",
]


def main() -> int:
    if DST.exists():
        shutil.rmtree(DST)
    om = DST / "seed_ollama_models"
    digests: set[str] = set()
    for m in OLLAMA_MANIFESTS:
        src = SRC_OLLAMA / m
        if not src.exists():
            print(f"MISSING manifest: {src}")
            return 1
        dst = om / m
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        data = json.loads(src.read_text(encoding="utf-8"))
        cfg = data.get("config") or {}
        if "digest" in cfg:
            digests.add(cfg["digest"])
        for layer in data.get("layers", []):
            digests.add(layer["digest"])
    blobs_dst = om / "blobs"
    blobs_dst.mkdir(parents=True, exist_ok=True)
    blob_bytes = 0
    for d in sorted(digests):
        if not d.startswith("sha256:"):
            print(f"unexpected digest algo: {d}")
            return 1
        name = d.replace(":", "-")
        s = SRC_OLLAMA / "blobs" / name
        if not s.exists():
            print(f"MISSING blob: {name}")
            return 1
        shutil.copy2(s, blobs_dst / name)
        blob_bytes += s.stat().st_size
    meta_src = SRC_OLLAMA / "metadata"
    if meta_src.exists():
        shutil.copytree(meta_src, om / "metadata", dirs_exist_ok=True)
    print(f"ollama seed: {len(digests)} blobs, {blob_bytes / 1e9:.3f} GB")
    for m in OLLAMA_MANIFESTS:
        print(f"  manifest {m}")
    hf_bytes = 0
    for r in HF_REPOS:
        s = SRC_HF / r
        if not s.exists():
            print(f"MISSING hf repo: {s}")
            return 1
        shutil.copytree(s, DST / "seed_hf_hub" / r, dirs_exist_ok=True)
        hf_bytes += sum(f.stat().st_size for f in (DST / "seed_hf_hub" / r).rglob("*") if f.is_file())
    print(f"hf seed: {len(HF_REPOS)} repos, {hf_bytes / 1e9:.3f} GB")
    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print(f"staging total: {total / 1e9:.3f} GB at {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
