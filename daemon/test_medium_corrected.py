import os
import sys
import time

cuda_path = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
if cuda_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = cuda_path + ";" + os.environ.get("PATH", "")

from faster_whisper import WhisperModel
from blankslate.voice.text_correction import correct_text

model = WhisperModel("models/faster-whisper-medium.en", device="cuda", compute_type="float16")

test_dir = "test_live_accent"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

# Expected commands per clip
expected_map = {
    "cmd_01.wav": "open word",
    "cmd_02.wav": "open notepad",
    "cmd_03.wav": "hey blankslate open chrome",
    "cmd_04.wav": "hey blank open brave",
    "cmd_05.wav": "blank open word",
    "cmd_06.wav": "open paint",
    "cmd_07.wav": "switch to chrome",
    "cmd_08.wav": "minimize window",
}

print("MEDIUM.EN + CORRECTION LAYER")
print("=" * 110)

results = []
for clip in clips:
    path = os.path.join(test_dir, clip)
    start = time.time()
    segments, info = model.transcribe(path, language="en", beam_size=5)
    raw_text = " ".join([s.text for s in segments]).strip()
    latency = (time.time() - start) * 1000

    corrected = correct_text(raw_text)
    expected = expected_map.get(clip, "")

    match_raw = expected.lower() in raw_text.lower() or raw_text.lower() in expected.lower()
    match_corr = expected.lower() in corrected.lower() or corrected.lower() in expected.lower()

    results.append((clip, raw_text, corrected, expected, match_raw, match_corr, latency))

    raw_s = "Y" if match_raw else "N"
    corr_s = "Y" if match_corr else "N"
    line = "{:<20} {:<35} {:<35} {:<30} raw={} corr={}".format(
        clip, raw_text, corrected, expected, raw_s, corr_s
    )
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.flush()

raw_correct = sum(1 for r in results if r[4])
corr_correct = sum(1 for r in results if r[5])
avg_latency = sum(r[6] for r in results) / len(results)

print("=" * 110)
print("Raw accuracy: {}/{} ({:.0f}%)".format(raw_correct, len(results), raw_correct/len(results)*100))
print("After correction: {}/{} ({:.0f}%)".format(corr_correct, len(results), corr_correct/len(results)*100))
print("Avg latency: {:.0f}ms".format(avg_latency))
