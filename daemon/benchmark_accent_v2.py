"""Benchmark: small.en vs small on Indian English accent test clips."""
import os, sys, time, json, wave, re
sys.path.insert(0, os.path.dirname(__file__))

# Ensure CUDA DLL path
import os as _os
cuda_dll = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
_os.add_dll_directory(cuda_dll)
_os.environ["PATH"] = cuda_dll + ";" + _os.environ.get("PATH", "")

from faster_whisper import WhisperModel

TEST_DIR = os.path.join(os.path.dirname(__file__), "test_audio_accent_v2")

# Expected transcripts (ground truth)
EXPECTED = {
    "01_open_notepad": "Hey BlankSlate, open notepad",
    "02_open_chrome": "Blank Slate, open the browser please",
    "03_launch_word": "Hey assistant, open Microsoft Word",
    "04_start_calculator": "Blank Slate, start the calculator",
    "05_open_paint": "Open paint application",
    "06_switch_window": "Switch to the Chrome window",
    "07_list_windows": "Show me all the open windows",
    "08_minimize_window": "Minimize the current window",
    "09_type_hello": "Type hello world in the document",
    "10_dictate_sentence": "The quick brown fox jumps over the lazy dog",
    "11_whats_time": "What is the current time",
    "12_whats_date": "What is today's date",
    "13_good_morning": "Good morning, how are you doing today",
    "14_weather_query": "What is the weather like in Mumbai right now",
    "15_reminder": "Remind me to buy groceries after work",
    "16_note_taking": "Take a note that the meeting is at three PM",
    "17_search_query": "Search the web for best Indian restaurants near me",
    "18_math_calc": "What is 247 multiplied by 83",
    "19_general_chat": "Tell me a joke about programming",
    "20_complex_command": "Open notepad and type my daily report for today",
    "21_pronunciation_hard": "Launch Visual Studio Code",
    "22_numbers": "Set a timer for fifteen minutes and thirty seconds",
    "23_email": "Open my email and check for new messages",
    "24_file_management": "Open file manager and navigate to downloads folder",
    "25_system": "What is the battery percentage on my laptop",
}

# Wake words to check separately
WAKE_WORDS = ["blankslate", "blank slate", "hey blankslate", "hey assistant"]


def similarity(a: str, b: str) -> float:
    """Simple word-level accuracy: fraction of expected words found in transcription."""
    a_words = set(re.findall(r'\w+', a.lower()))
    b_words = set(re.findall(r'\w+', b.lower()))
    if not a_words:
        return 0.0
    return len(a_words & b_words) / len(a_words)


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())


def test_model(model_name: str, compute_type: str = "float16") -> dict:
    print(f"\n{'='*60}")
    print(f"Testing: {model_name} ({compute_type})")
    print(f"{'='*60}")

    model = WhisperModel(model_name, device="cuda", compute_type=compute_type)

    results = []
    total_latency = 0

    for clip_name, expected_text in sorted(EXPECTED.items()):
        wav_path = os.path.join(TEST_DIR, f"{clip_name}.wav")
        if not os.path.exists(wav_path):
            continue

        audio_bytes = read_wav(wav_path)
        import numpy as np
        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # Warm up first clip
        if clip_name == "01_open_notepad":
            list(model.transcribe(audio_np, beam_size=3, language="en", vad_filter=True))

        t0 = time.perf_counter()
        segments, info = model.transcribe(
            audio_np, beam_size=3, language="en", vad_filter=True,
            initial_prompt="BlankSlate, open notepad, Chrome, Cursor, Visual Studio Code, switch window, type text, list windows, get time, hey BlankSlate, Blank Slate assistant",
        )
        texts = []
        for seg in segments:
            texts.append(seg.text)
        latency_ms = (time.perf_counter() - t0) * 1000
        total_latency += latency_ms

        transcript = " ".join(texts).strip()
        acc = similarity(expected_text, transcript)

        # Check wake word accuracy
        wake_found = any(w in transcript.lower() for w in WAKE_WORDS)

        results.append({
            "clip": clip_name,
            "expected": expected_text,
            "transcript": transcript,
            "accuracy": acc,
            "latency_ms": latency_ms,
            "wake_word": wake_found,
        })

        status = "OK" if acc >= 0.7 else "MISS"
        wake = "W" if wake_found else "-"
        print(f"  [{status}] [{wake}] {clip_name}: {acc:.0%} ({latency_ms:.0f}ms)")
        print(f"         Expected: {expected_text}")
        print(f"         Got:      {transcript}")

    avg_latency = total_latency / len(results) if results else 0
    avg_accuracy = sum(r["accuracy"] for r in results) / len(results) if results else 0
    wake_accuracy = sum(1 for r in results if r["wake_word"]) / len(results) if results else 0

    # Category breakdown
    cmd_clips = [r for r in results if r["clip"].startswith(("01", "02", "03", "04", "05", "06", "07", "08", "09", "20", "21"))]
    conv_clips = [r for r in results if r["clip"].startswith(("10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "22", "23", "24", "25"))]

    cmd_acc = sum(r["accuracy"] for r in cmd_clips) / len(cmd_clips) if cmd_clips else 0
    conv_acc = sum(r["accuracy"] for r in conv_clips) / len(conv_clips) if conv_clips else 0

    summary = {
        "model": model_name,
        "compute_type": compute_type,
        "total_clips": len(results),
        "avg_accuracy": avg_accuracy,
        "cmd_accuracy": cmd_acc,
        "conv_accuracy": conv_acc,
        "wake_word_accuracy": wake_accuracy,
        "avg_latency_ms": avg_latency,
        "results": results,
    }

    print(f"\n  SUMMARY: {model_name}")
    print(f"    Overall accuracy:    {avg_accuracy:.0%}")
    print(f"    Command accuracy:    {cmd_acc:.0%}")
    print(f"    Conversation acc:    {conv_acc:.0%}")
    print(f"    Wake word accuracy:  {wake_accuracy:.0%}")
    print(f"    Avg latency:         {avg_latency:.0f}ms")

    return summary


def main():
    results = {}
    results["small.en"] = test_model("small.en", "float16")
    results["small"] = test_model("small", "float16")

    # Comparison
    print(f"\n{'='*60}")
    print(f"COMPARISON: small.en vs small (Indian English)")
    print(f"{'='*60}")

    for metric in ["avg_accuracy", "cmd_accuracy", "conv_accuracy", "wake_word_accuracy", "avg_latency_ms"]:
        v1 = results["small.en"][metric]
        v2 = results["small"][metric]
        if metric == "avg_latency_ms":
            diff = v1 - v2
            better = "small" if v2 < v1 else "small.en"
        else:
            diff = v2 - v1
            better = "small" if v2 > v1 else "small.en"
        print(f"  {metric:25s}  small.en={v1:.0% if 'acc' in metric else v1:.0f}ms  small={v2:.0% if 'acc' in metric else v2:.0f}ms  -> {better} wins by {abs(diff):.0% if 'acc' in metric else abs(diff):.0f}ms")

    # Save results
    out = os.path.join(os.path.dirname(__file__), "accent_benchmark_results_v2.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {out}")


if __name__ == "__main__":
    main()
