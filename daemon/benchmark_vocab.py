"""Phase 3: Benchmark vocabulary priming — WITH vs WITHOUT.

Tests small.en GPU float16 against both general and accent test sets,
with and without the vocabulary prompt.
"""
import os
import sys
import io
import time
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']

# Vocabulary prompt (same as config.py)
VOCAB_PROMPT = (
    "BlankSlate, open notepad, Chrome, Cursor, Visual Studio Code, "
    "switch window, type text, list windows, get time, "
    "hey BlankSlate, Blank Slate assistant"
)

# General test clips (Phase 1)
GENERAL_CLIPS = [
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

# Accent test clips
ACCENT_CLIPS = [
    ("accent_01_wake_alone.wav", "Blank Slate", "Wake word alone"),
    ("accent_02_wake_command.wav", "Blank Slate, open notepad", "Wake + command"),
    ("accent_03_retroflex_t.wav", "switch to Chrome and type the text", "Retroflex T/D"),
    ("accent_05_app_names.wav", "open Visual Studio Code and switch to Chrome", "App names"),
    ("accent_06_longer_command.wav", "Blank Slate, can you please open the notepad application and type hello world in it", "Longer command"),
    ("accent_09_proper_noun.wav", "switch to Cursor and open the terminal", "Proper noun: Cursor"),
]


def run_benchmark(model, clips, test_dir, label):
    """Run benchmark for a model against a set of clips."""
    results = []
    for filename, expected, desc in clips:
        filepath = os.path.join(test_dir, filename)
        if not os.path.exists(filepath):
            continue

        start = time.time()
        segments, info = model.transcribe(filepath, language="en")
        segments = list(segments)
        latency = time.time() - start
        transcription = " ".join(seg.text.strip() for seg in segments)

        expected_lower = expected.lower()
        transcription_lower = transcription.lower()
        key_words = expected_lower.split()
        matched = sum(1 for w in key_words if w in transcription_lower)
        accuracy = matched / len(key_words) if key_words else 0
        bs_ok = "blankslate" in transcription_lower.replace(" ", "") or "blank slate" in transcription_lower
        chrome_ok = "chrome" in transcription_lower

        results.append({
            "filename": filename,
            "expected": expected,
            "transcription": transcription,
            "latency": latency,
            "accuracy": accuracy,
            "blankslate": bs_ok,
            "chrome": chrome_ok,
        })

    return results


def print_results(label, results):
    """Print results for a benchmark run."""
    print(f"\n{'='*70}")
    print(f"{label}")
    print(f"{'='*70}")

    for r in results:
        bs = "✅" if r["blankslate"] else "❌"
        chrome = "✅" if r["chrome"] else "❌"
        print(f"  {r['filename']:<35} {r['latency']:.3f}s | {r['transcription']!r}")
        print(f"  {'':35} acc={r['accuracy']:.0%} bs={bs} chrome={chrome}")

    avg_lat = sum(r["latency"] for r in results) / len(results)
    avg_acc = sum(r["accuracy"] for r in results) / len(results)
    bs_pass = sum(1 for r in results if r["blankslate"])
    chrome_pass = sum(1 for r in results if r["chrome"])

    print(f"\n  SUMMARY: avg_lat={avg_lat:.3f}s, avg_acc={avg_acc:.0%}, BS={bs_pass}/{len(results)}, Chrome={chrome_pass}/{len(results)}")
    return {"avg_lat": avg_lat, "avg_acc": avg_acc, "bs": bs_pass, "chrome": chrome_pass, "n": len(results)}


def main():
    from faster_whisper import WhisperModel

    general_dir = os.path.join(os.path.dirname(__file__), "test_audio")
    accent_dir = os.path.join(os.path.dirname(__file__), "test_audio_accent")

    print("=" * 70)
    print("PHASE 3: VOCABULARY PRIMING BENCHMARK")
    print("=" * 70)
    print(f"Model: small.en (float16, GPU)")
    print(f"Vocabulary prompt: {VOCAB_PROMPT!r}")

    all_results = {}

    # ── WITHOUT priming ──
    print("\nLoading model WITHOUT vocabulary prompt...")
    model_no_prompt = WhisperModel("small.en", device="cuda", compute_type="float16")

    # General set
    results = run_benchmark(model_no_prompt, GENERAL_CLIPS, general_dir, "no_prompt_general")
    summary = print_results("WITHOUT Prompt — General Set", results)
    all_results["no_prompt_general"] = summary

    # Accent set
    results = run_benchmark(model_no_prompt, ACCENT_CLIPS, accent_dir, "no_prompt_accent")
    summary = print_results("WITHOUT Prompt — Accent Set", results)
    all_results["no_prompt_accent"] = summary

    del model_no_prompt

    # ── WITH priming ──
    print("\nLoading model WITH vocabulary prompt...")
    model_with_prompt = WhisperModel("small.en", device="cuda", compute_type="float16")

    # General set
    results = run_benchmark(model_with_prompt, GENERAL_CLIPS, general_dir, "with_prompt_general")
    summary = print_results("WITH Prompt — General Set", results)
    all_results["with_prompt_general"] = summary

    # Accent set
    results = run_benchmark(model_with_prompt, ACCENT_CLIPS, accent_dir, "with_prompt_accent")
    summary = print_results("WITH Prompt — Accent Set", results)
    all_results["with_prompt_accent"] = summary

    del model_with_prompt

    # ── Comparison table ──
    print("\n" + "=" * 70)
    print("COMPARISON: WITH vs WITHOUT Vocabulary Priming")
    print("=" * 70)
    print(f"{'Condition':<30} {'Avg Lat':>8} {'Accuracy':>8} {'BS':>5} {'Chrome':>7}")
    print("-" * 70)

    for key, label in [
        ("no_prompt_general", "WITHOUT — General"),
        ("with_prompt_general", "WITH — General"),
        ("no_prompt_accent", "WITHOUT — Accent"),
        ("with_prompt_accent", "WITH — Accent"),
    ]:
        r = all_results[key]
        print(f"{label:<30} {r['avg_lat']:>7.3f}s {r['avg_acc']:>7.0%} {r['bs']}/{r['n']} {r['chrome']}/{r['n']}")

    print("=" * 70)

    # Save results
    results_path = os.path.join(os.path.dirname(__file__), "vocab_priming_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    main()
