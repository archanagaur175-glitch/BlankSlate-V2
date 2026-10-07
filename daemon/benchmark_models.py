"""Phase 2: Benchmark all GPU model candidates against test audio set.

Tests: tiny.en, base.en, small.en, distil-small.en, distil-medium.en, medium.en, large-v3
All on GPU with float16 compute type.
"""
import os
import sys
import io
import time
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Add Ollama's CUDA directory for cuBLAS
cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']

# Test audio definitions
TEST_CLIPS = [
    ("01_wake_word.wav", "Blank Slate", "Wake word only"),
    ("02_wake_command.wav", "Blank Slate, open notepad", "Wake word + command"),
    ("03_command_only.wav", "Open notepad", "Command without wake word"),
    ("04_switch_window.wav", "Switch to Chrome", "Window switching command"),
    ("05_type_text.wav", "Type hello world", "Text injection command"),
    ("06_list_windows.wav", "List all open windows", "Query command"),
    ("07_get_time.wav", "What time is it", "Time query"),
    ("08_background_noise.wav", "Blank Slate, what's the weather like today", "Longer command"),
    ("09_pause_speech.wav", "Blank Slate... open notepad", "Command with pause"),
    ("10_adversarial.wav", "Hey Blank Slate, can you open the notepad application please", "Adversarial phonetic"),
]

# Models to benchmark
MODELS = [
    ("tiny.en", "tiny.en", "float16"),
    ("base.en", "base.en", "float16"),
    ("small.en", "small.en", "float16"),
    ("distil-small.en", "distil-whisper/distil-small.en", "float16"),
    ("distil-medium.en", "distil-whisper/distil-medium.en", "float16"),
    ("medium.en", "medium.en", "float16"),
    ("large-v3", "large-v3", "float16"),
]


def benchmark_model(model_name, model_id, compute_type, test_dir):
    """Benchmark a single model against all test clips."""
    from faster_whisper import WhisperModel

    print(f"\n{'='*70}")
    print(f"BENCHMARKING: {model_name} ({compute_type}, GPU)")
    print(f"{'='*70}")

    # Load model
    print(f"Loading {model_name}...")
    start = time.time()
    try:
        model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
        load_time = time.time() - start
        print(f"Model loaded in {load_time:.2f}s")
    except Exception as e:
        print(f"FAILED to load: {e}")
        return None

    results = []

    for filename, expected_text, desc in TEST_CLIPS:
        filepath = os.path.join(test_dir, filename)
        if not os.path.exists(filepath):
            print(f"\n⚠️  {filename}: file not found, skipping")
            continue

        print(f"\n--- {filename}: {desc} ---")
        print(f"  Expected: {expected_text!r}")

        # Transcribe
        start = time.time()
        segments, info = model.transcribe(filepath, language="en", initial_prompt="BlankSlate")
        segments = list(segments)
        latency = time.time() - start

        # Collect transcription
        transcription = " ".join(seg.text.strip() for seg in segments)
        print(f"  Got:      {transcription!r}")
        print(f"  Latency:  {latency:.3f}s")

        # Check correctness
        expected_lower = expected_text.lower()
        transcription_lower = transcription.lower()
        key_words = expected_lower.split()
        matched_words = sum(1 for w in key_words if w in transcription_lower)
        accuracy = matched_words / len(key_words) if key_words else 0

        blankslate_correct = (
            "blankslate" in transcription_lower.replace(" ", "") or
            "blank slate" in transcription_lower
        )

        print(f"  Accuracy: {accuracy:.0%}, BlankSlate: {'✅' if blankslate_correct else '❌'}")

        results.append({
            "filename": filename,
            "description": desc,
            "expected": expected_text,
            "transcription": transcription,
            "latency": latency,
            "accuracy": accuracy,
            "blankslate_correct": blankslate_correct,
        })

    # Summary for this model
    total_latency = sum(r["latency"] for r in results)
    avg_latency = total_latency / len(results) if results else 0
    avg_accuracy = sum(r["accuracy"] for r in results) / len(results) if results else 0
    blankslate_pass = sum(1 for r in results if r["blankslate_correct"])

    print(f"\n--- {model_name} Summary ---")
    print(f"Avg latency: {avg_latency:.3f}s")
    print(f"Avg accuracy: {avg_accuracy:.0%}")
    print(f"BlankSlate: {blankslate_pass}/{len(results)} pass")

    # Cleanup
    del model

    return {
        "model_name": model_name,
        "model_id": model_id,
        "compute_type": compute_type,
        "load_time": load_time,
        "avg_latency": avg_latency,
        "avg_accuracy": avg_accuracy,
        "blankslate_pass": blankslate_pass,
        "total_clips": len(results),
        "results": results,
    }


def run_all_benchmarks():
    """Run benchmarks for all model candidates."""
    test_dir = os.path.join(os.path.dirname(__file__), "test_audio")

    print("=" * 70)
    print("PHASE 2 BENCHMARK — GPU Model Candidates")
    print("=" * 70)
    print(f"Test clips: {len(TEST_CLIPS)}")
    print(f"Models: {len(MODELS)}")
    print(f"Device: GPU (CUDA via Ollama cuBLAS)")

    all_results = []

    for model_name, model_id, compute_type in MODELS:
        result = benchmark_model(model_name, model_id, compute_type, test_dir)
        if result:
            all_results.append(result)

    # Comparison table
    print("\n" + "=" * 70)
    print("COMPARISON TABLE")
    print("=" * 70)
    print(f"{'Model':<20} {'Load':>6} {'Avg Lat':>8} {'Accuracy':>8} {'BS Pass':>8}")
    print("-" * 70)

    # Add baseline row
    print(f"{'small.en (CPU)':<20} {'1.81s':>6} {'2.194s':>8} {'91%':>8} {'4/10':>8}")

    for r in all_results:
        print(f"{r['model_name']:<20} {r['load_time']:>5.1f}s {r['avg_latency']:>7.3f}s {r['avg_accuracy']:>7.0%} {r['blankslate_pass']}/{r['total_clips']}")

    print("=" * 70)

    # Save results
    results_path = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    run_all_benchmarks()
