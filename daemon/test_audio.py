"""Phase 1: Generate test audio set and run baseline benchmark.

Uses Windows SAPI TTS to create 8-10 test clips, then benchmarks
the current small.en CPU configuration against them.
"""
import os
import sys
import io
import time
import wave
import struct

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Test audio definitions: (filename, text, description)
TEST_CLIPS = [
    ("01_wake_word.wav", "Blank Slate", "Wake word only"),
    ("02_wake_command.wav", "Blank Slate, open notepad", "Wake word + command"),
    ("03_command_only.wav", "Open notepad", "Command without wake word"),
    ("04_switch_window.wav", "Switch to Chrome", "Window switching command"),
    ("05_type_text.wav", "Type hello world", "Text injection command"),
    ("06_list_windows.wav", "List all open windows", "Query command"),
    ("07_get_time.wav", "What time is it", "Time query"),
    ("08_background_noise.wav", "Blank Slate, what's the weather like today", "Longer command (noise simulation)"),
    ("09_pause_speech.wav", "Blank Slate... open notepad", "Command with pause"),
    ("10_adversarial.wav", "Hey Blank Slate, can you open the notepad application please", "Adversarial phonetic"),
]


def generate_test_audio():
    """Generate test audio clips using Windows SAPI TTS."""
    from blankslate.voice.tts import TTS

    tts = TTS()
    if not tts.is_ready:
        print("ERROR: TTS not ready")
        return False

    test_dir = os.path.join(os.path.dirname(__file__), "test_audio")
    os.makedirs(test_dir, exist_ok=True)

    print(f"Generating {len(TEST_CLIPS)} test clips in {test_dir}...")
    for filename, text, desc in TEST_CLIPS:
        filepath = os.path.join(test_dir, filename)
        wav_bytes = tts.synthesize(text)
        if wav_bytes:
            with open(filepath, "wb") as f:
                f.write(wav_bytes)
            print(f"  ✅ {filename}: {desc} ({len(wav_bytes)} bytes)")
        else:
            print(f"  ❌ {filename}: FAILED to generate")
            return False

    print(f"\nTest audio set generated: {len(TEST_CLIPS)} clips")
    return True


def run_baseline_benchmark():
    """Benchmark small.en CPU against test audio set."""
    from faster_whisper import WhisperModel

    test_dir = os.path.join(os.path.dirname(__file__), "test_audio")
    if not os.path.exists(test_dir):
        print("ERROR: test_audio directory not found")
        return

    print("\n" + "=" * 70)
    print("PHASE 1 BASELINE BENCHMARK — small.en CPU int8")
    print("=" * 70)

    # Load model
    print("\nLoading small.en model (CPU, int8)...")
    start = time.time()
    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    load_time = time.time() - start
    print(f"Model loaded in {load_time:.2f}s")

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
        print(f"  Language: {info.language} ({info.language_probability:.2f})")

        # Check correctness (simple substring matching)
        expected_lower = expected_text.lower()
        transcription_lower = transcription.lower()

        # Check if key words are present
        key_words = expected_lower.split()
        matched_words = sum(1 for w in key_words if w in transcription_lower)
        accuracy = matched_words / len(key_words) if key_words else 0

        # Special check for "BlankSlate" / "Blank Slate"
        blankslate_correct = (
            "blankslate" in transcription_lower.replace(" ", "") or
            "blank slate" in transcription_lower
        )

        print(f"  Words matched: {matched_words}/{len(key_words)} ({accuracy:.0%})")
        print(f"  BlankSlate correct: {blankslate_correct}")

        results.append({
            "filename": filename,
            "description": desc,
            "expected": expected_text,
            "transcription": transcription,
            "latency": latency,
            "accuracy": accuracy,
            "blankslate_correct": blankslate_correct,
            "language_prob": info.language_probability,
        })

    # Summary table
    print("\n" + "=" * 70)
    print("BASELINE RESULTS SUMMARY")
    print("=" * 70)
    print(f"{'File':<25} {'Latency':>8} {'Accuracy':>8} {'BlankSlate':>10}")
    print("-" * 70)

    total_latency = 0
    total_accuracy = 0
    blankslate_pass = 0

    for r in results:
        total_latency += r["latency"]
        total_accuracy += r["accuracy"]
        if r["blankslate_correct"]:
            blankslate_pass += 1

        status = "✅" if r["blankslate_correct"] else "❌"
        print(f"{r['filename']:<25} {r['latency']:>7.3f}s {r['accuracy']:>7.0%} {status:>10}")

    n = len(results)
    print("-" * 70)
    print(f"{'AVERAGE':<25} {total_latency/n:>7.3f}s {total_accuracy/n:>7.0%} {blankslate_pass}/{n} pass")
    print(f"{'TOTAL':<25} {total_latency:>7.3f}s")
    print("=" * 70)

    # Save results to file
    results_path = os.path.join(os.path.dirname(__file__), "baseline_results.txt")
    with open(results_path, "w") as f:
        f.write("PHASE 1 BASELINE BENCHMARK — small.en CPU int8\n")
        f.write("=" * 70 + "\n\n")
        for r in results:
            f.write(f"File: {r['filename']}\n")
            f.write(f"  Description: {r['description']}\n")
            f.write(f"  Expected: {r['expected']!r}\n")
            f.write(f"  Got: {r['transcription']!r}\n")
            f.write(f"  Latency: {r['latency']:.3f}s\n")
            f.write(f"  Accuracy: {r['accuracy']:.0%}\n")
            f.write(f"  BlankSlate correct: {r['blankslate_correct']}\n\n")
        f.write(f"SUMMARY: avg latency={total_latency/n:.3f}s, avg accuracy={total_accuracy/n:.0%}, BlankSlate={blankslate_pass}/{n}\n")

    print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    if "--generate" in sys.argv:
        generate_test_audio()
    elif "--benchmark" in sys.argv:
        run_baseline_benchmark()
    else:
        print("Usage:")
        print("  python test_audio.py --generate   # Generate test audio clips")
        print("  python test_audio.py --benchmark  # Run baseline benchmark")
