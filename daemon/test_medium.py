import os
import sys
import time

cuda_path = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
if cuda_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = cuda_path + ";" + os.environ.get("PATH", "")

from faster_whisper import WhisperModel

print("Loading medium.en model on GPU...")
model = WhisperModel("models/faster-whisper-medium.en", device="cuda", compute_type="float16")
print("Model loaded.\n")

# Check VRAM
try:
    import torch
    if torch.cuda.is_available():
        vram_used = torch.cuda.memory_allocated() / 1024 / 1024
        vram_reserved = torch.cuda.memory_reserved() / 1024 / 1024
        print(f"VRAM used: {vram_used:.0f}MB / Reserved: {vram_reserved:.0f}MB")
except:
    pass

test_dir = "test_live_accent"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

print("\n" + "=" * 70)
print("TESTING MEDIUM.EN ON LIVE VOICE CLIPS")
print("=" * 70)
print(f"{'Clip':<25} {'medium.en':<40} {'Expected'}")
print("-" * 70)

results = []
for clip in clips:
    path = os.path.join(test_dir, clip)
    
    start = time.time()
    segments, info = model.transcribe(path, language="en", beam_size=5)
    text = " ".join([s.text for s in segments]).strip()
    latency = (time.time() - start) * 1000
    
    expected = clip.replace(".wav", "").replace("_", " ")[3:]
    
    results.append({
        "clip": clip,
        "medium": text,
        "expected": expected,
        "latency": latency,
    })
    
    match = expected.lower() in text.lower() or text.lower() in expected.lower()
    status = "✓" if match else "✗"
    
    sys.stdout.buffer.write(f"{clip:<25} {text:<40} {expected} {status}\n".encode("utf-8"))
    sys.stdout.flush()

print("=" * 70)
correct = sum(1 for r in results if r['expected'].lower() in r['medium'].lower() or r['medium'].lower() in r['expected'].lower())
print(f"Accuracy: {correct}/{len(results)}")
print(f"Avg latency: {sum(r['latency'] for r in results)/len(results):.0f}ms")
