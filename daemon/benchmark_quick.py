"""Phase 2: Quick benchmark — smaller models first."""
import os
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Add Ollama's CUDA directory
cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']

TEST_CLIPS = [
    ("01_wake_word.wav", "Blank Slate"),
    ("02_wake_command.wav", "Blank Slate, open notepad"),
    ("04_switch_window.wav", "Switch to Chrome"),
    ("08_background_noise.wav", "Blank Slate, what's the weather like today"),
    ("10_adversarial.wav", "Hey Blank Slate, can you open the notepad application please"),
]

MODELS = [
    ("tiny.en", "tiny.en", "float16"),
    ("base.en", "base.en", "float16"),
    ("small.en", "small.en", "float16"),
]

test_dir = os.path.join(os.path.dirname(__file__), "test_audio")

from faster_whisper import WhisperModel

results = {}

for model_name, model_id, compute_type in MODELS:
    print(f"\n{'='*50}")
    print(f"Model: {model_name} ({compute_type})")
    print(f"{'='*50}")

    start = time.time()
    model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
    load_time = time.time() - start
    print(f"Loaded in {load_time:.2f}s")

    model_results = []
    for filename, expected in TEST_CLIPS:
        filepath = os.path.join(test_dir, filename)
        start = time.time()
        segments, info = model.transcribe(filepath, language="en", initial_prompt="BlankSlate")
        segments = list(segments)
        latency = time.time() - start
        transcription = " ".join(seg.text.strip() for seg in segments)

        expected_lower = expected.lower()
        transcription_lower = transcription.lower()
        key_words = expected_lower.split()
        matched = sum(1 for w in key_words if w in transcription_lower)
        accuracy = matched / len(key_words) if key_words else 0
        bs_ok = "blankslate" in transcription_lower.replace(" ", "") or "blank slate" in transcription_lower

        print(f"  {filename}: {latency:.3f}s | {transcription!r} | acc={accuracy:.0%} | bs={'✅' if bs_ok else '❌'}")
        model_results.append({"latency": latency, "accuracy": accuracy, "blankslate": bs_ok})

    avg_lat = sum(r["latency"] for r in model_results) / len(model_results)
    avg_acc = sum(r["accuracy"] for r in model_results) / len(model_results)
    bs_pass = sum(1 for r in model_results if r["blankslate"])
    print(f"  AVG: {avg_lat:.3f}s, {avg_acc:.0%}, BS={bs_pass}/{len(model_results)}")
    results[model_name] = {"load": load_time, "avg_lat": avg_lat, "avg_acc": avg_acc, "bs": bs_pass}
    del model

print("\n" + "=" * 50)
print("QUICK COMPARISON")
print("=" * 50)
print(f"{'Model':<12} {'Load':>6} {'Lat':>6} {'Acc':>5} {'BS':>5}")
for name, r in results.items():
    print(f"{name:<12} {r['load']:>5.1f}s {r['avg_lat']:>5.3f}s {r['avg_acc']:>4.0%} {r['bs']}/5")
