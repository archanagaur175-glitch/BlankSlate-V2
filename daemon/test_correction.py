import os
import sys

cuda_path = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
if cuda_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = cuda_path + ";" + os.environ.get("PATH", "")

from faster_whisper import WhisperModel
from blankslate.voice.text_correction import correct_text

# Load model
model = WhisperModel("small.en", device="cuda", compute_type="float16")

# Test on recorded clips
test_dir = "test_live_accent"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

print("=" * 80)
print("TESTING FUZZY CORRECTION ON RECORDED CLIPS")
print("=" * 80)
print(f"{'Clip':<20} {'Raw STT':<35} {'Corrected':<35}")
print("-" * 80)

for clip in clips:
    path = os.path.join(test_dir, clip)
    segments, info = model.transcribe(path, language="en", beam_size=5)
    raw_text = " ".join([s.text for s in segments]).strip()
    corrected = correct_text(raw_text)
    
    # Check if correction helped
    match_raw = any(expected in raw_text.lower() for expected in ["open", "word", "notepad", "chrome", "blank", "paint", "switch", "minimize"])
    match_corrected = any(expected in corrected.lower() for expected in ["open", "word", "notepad", "chrome", "blank", "paint", "switch", "minimize"])
    
    status = "✓" if match_corrected else "✗"
    sys.stdout.buffer.write(f"{clip:<20} {raw_text:<35} {corrected:<35} {status}\n".encode("utf-8"))
    sys.stdout.flush()

print("=" * 80)
