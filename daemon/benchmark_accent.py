"""Phase 2b: Benchmark accent-stress test set."""
import os
import sys
import io
import time
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']

# Accent test clips
ACCENT_CLIPS = [
    ("accent_01_wake_alone.wav", "Blank Slate", "Wake word alone"),
    ("accent_02_wake_command.wav", "Blank Slate, open notepad", "Wake + command"),
    ("accent_03_retroflex_t.wav", "switch to Chrome and type the text", "Retroflex T/D"),
    ("accent_04_consonant_cluster.wav", "list all the open windows and switch between them", "Consonant clusters"),
    ("accent_05_app_names.wav", "open Visual Studio Code and switch to Chrome", "App names"),
    ("accent_06_longer_command.wav", "Blank Slate, can you please open the notepad application and type hello world in it", "Longer command"),
    ("accent_07_conversational.wav", "hey Blank Slate what time is it right now", "Conversational"),
    ("accent_08_noise_accent.wav", "Blank Slate open the Chrome browser and search for something", "Noise + accent"),
    ("accent_09_proper_noun.wav", "switch to Cursor and open the terminal", "Proper noun: Cursor"),
    ("accent_10_full_command.wav", "Blank Slate, list all the open windows, then switch to notepad and type some text", "Complex command"),
]

MODELS = [
    ("tiny.en", "tiny.en", "float16"),
    ("small.en", "small.en", "float16"),
    ("medium.en", "medium.en", "float16"),
]

test_dir = os.path.join(os.path.dirname(__file__), "test_audio_accent")

from faster_whisper import WhisperModel

all_results = {}

for model_name, model_id, compute_type in MODELS:
    print(f"\n{'='*70}")
    print(f"ACCENT BENCHMARK: {model_name} ({compute_type}, GPU)")
    print(f"{'='*70}")

    start = time.time()
    model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
    load_time = time.time() - start
    print(f"Loaded in {load_time:.2f}s")

    model_results = []
    for filename, expected, desc in ACCENT_CLIPS:
        filepath = os.path.join(test_dir, filename)
        if not os.path.exists(filepath):
            print(f"\n⚠️  {filename}: not found, skipping")
            continue

        print(f"\n--- {filename}: {desc} ---")
        print(f"  Expected: {expected!r}")

        start = time.time()
        segments, info = model.transcribe(filepath, language="en", initial_prompt="BlankSlate")
        segments = list(segments)
        latency = time.time() - start
        transcription = " ".join(seg.text.strip() for seg in segments)

        print(f"  Got:      {transcription!r}")
        print(f"  Latency:  {latency:.3f}s")

        # Check correctness
        expected_lower = expected.lower()
        transcription_lower = transcription.lower()
        key_words = expected_lower.split()
        matched = sum(1 for w in key_words if w in transcription_lower)
        accuracy = matched / len(key_words) if key_words else 0
        bs_ok = "blankslate" in transcription_lower.replace(" ", "") or "blank slate" in transcription_lower

        # Check Chrome specifically
        chrome_ok = "chrome" in transcription_lower

        print(f"  Accuracy: {accuracy:.0%}, BlankSlate: {'✅' if bs_ok else '❌'}, Chrome: {'✅' if chrome_ok else '❌'}")

        model_results.append({
            "filename": filename,
            "description": desc,
            "expected": expected,
            "transcription": transcription,
            "latency": latency,
            "accuracy": accuracy,
            "blankslate": bs_ok,
            "chrome": chrome_ok,
        })

    # Summary for this model
    avg_lat = sum(r["latency"] for r in model_results) / len(model_results)
    avg_acc = sum(r["accuracy"] for r in model_results) / len(model_results)
    bs_pass = sum(1 for r in model_results if r["blankslate"])
    chrome_pass = sum(1 for r in model_results if r["chrome"])

    print(f"\n--- {model_name} Accent Summary ---")
    print(f"Avg latency: {avg_lat:.3f}s")
    print(f"Avg accuracy: {avg_acc:.0%}")
    print(f"BlankSlate: {bs_pass}/{len(model_results)}")
    print(f"Chrome: {chrome_pass}/{len(model_results)}")

    all_results[model_name] = {
        "load_time": load_time,
        "avg_latency": avg_lat,
        "avg_accuracy": avg_acc,
        "blankslate_pass": bs_pass,
        "chrome_pass": chrome_pass,
        "results": model_results,
    }

    del model

# Comparison table
print("\n" + "=" * 70)
print("ACCENT BENCHMARK COMPARISON")
print("=" * 70)
print(f"{'Model':<12} {'Load':>6} {'Avg Lat':>8} {'Accuracy':>8} {'BS':>5} {'Chrome':>7}")
print("-" * 70)

# Phase 2 general set scores for comparison
phase2_scores = {
    "tiny.en": {"accuracy": 0.93, "bs": 4},
    "small.en": {"accuracy": 0.88, "bs": 3},
    "medium.en": {"accuracy": 0.91, "bs": 4},
}

for name, r in all_results.items():
    p2 = phase2_scores.get(name, {})
    p2_acc = p2.get("accuracy", 0)
    delta = r["avg_accuracy"] - p2_acc
    delta_str = f"({delta:+.0%})"
    print(f"{name:<12} {r['load_time']:>5.1f}s {r['avg_latency']:>7.3f}s {r['avg_accuracy']:>7.0%} {delta_str:>6} {r['blankslate_pass']}/10 {r['chrome_pass']}/10")

print("=" * 70)

# Save results
results_path = os.path.join(os.path.dirname(__file__), "accent_benchmark_results.json")
with open(results_path, "w") as f:
    json.dump(all_results, f, indent=2)
print(f"\nResults saved to: {results_path}")
