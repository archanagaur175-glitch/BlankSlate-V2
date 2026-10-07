import os
import sys
import time
import sounddevice as sd
import soundfile as sf
import numpy as np

cuda_path = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
if cuda_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = cuda_path + ";" + os.environ.get("PATH", "")

from faster_whisper import WhisperModel

# Load both models
print("Loading models...")
model_en = WhisperModel("small.en", device="cuda", compute_type="float16")
model_indian = WhisperModel("models/whisper-small-indian-accent-ct2", device="cuda", compute_type="float16")
print("Models loaded.\n")

SAMPLE_RATE = 16000
DURATION = 5  # seconds
TEST_DIR = "test_live_accent"
os.makedirs(TEST_DIR, exist_ok=True)

commands = [
    "open word",
    "open notepad",
    "hey blankslate open chrome",
    "hey blank open brave",
    "blank open word",
    "open paint",
    "switch to chrome",
    "minimize window",
]

def record_audio(duration, filename):
    print(f"Recording {duration}s... speak now!")
    audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='float32')
    sd.wait()
    sf.write(filename, audio, SAMPLE_RATE)
    print(f"Saved: {filename}")
    return filename

def transcribe_both(filepath):
    results = {}
    
    # small.en
    start = time.time()
    segments, info = model_en.transcribe(filepath, language="en", beam_size=5)
    text_en = " ".join([s.text for s in segments]).strip()
    latency_en = (time.time() - start) * 1000
    
    # Indian English
    start = time.time()
    segments, info = model_indian.transcribe(filepath, language="en", beam_size=5)
    text_indian = " ".join([s.text for s in segments]).strip()
    latency_indian = (time.time() - start) * 1000
    
    return text_en, latency_en, text_indian, latency_indian

print("=" * 70)
print("LIVE VOICE ACCENT TEST")
print("=" * 70)
print("Commands to say (one at a time):")
for i, cmd in enumerate(commands, 1):
    print(f"  {i}. \"{cmd}\"")
print()
print("Press ENTER to start recording each command.")
print("Type 'q' to quit early.")
print("=" * 70)

results = []

for i, expected in enumerate(commands, 1):
    input(f"\n[{i}/{len(commands)}] Press ENTER to record \"{expected}\" (or 'q' to quit): ")
    
    if input().strip().lower() == 'q':
        break
    
    filename = os.path.join(TEST_DIR, f"cmd_{i:02d}.wav")
    record_audio(DURATION, filename)
    
    text_en, lat_en, text_indian, lat_indian = transcribe_both(filename)
    
    results.append({
        "expected": expected,
        "small_en": text_en,
        "lat_en": lat_en,
        "indian": text_indian,
        "lat_indian": lat_indian,
    })
    
    print(f"\n  Expected:    \"{expected}\"")
    print(f"  small.en:    \"{text_en}\" ({lat_en:.0f}ms)")
    print(f"  Indian EN:   \"{text_indian}\" ({lat_indian:.0f}ms)")
    
    en_match = expected.lower() in text_en.lower() or text_en.lower() in expected.lower()
    in_match = expected.lower() in text_indian.lower() or text_indian.lower() in expected.lower()
    print(f"  Match:       small.en={'✓' if en_match else '✗'}  Indian={'✓' if in_match else '✗'}")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'Expected':<30} {'small.en':<30} {'Indian EN':<30}")
print("-" * 90)
for r in results:
    print(f"{r['expected']:<30} {r['small_en']:<30} {r['indian']:<30}")

en_correct = sum(1 for r in results if r['expected'].lower() in r['small_en'].lower() or r['small_en'].lower() in r['expected'].lower())
in_correct = sum(1 for r in results if r['expected'].lower() in r['indian'].lower() or r['indian'].lower() in r['expected'].lower())
print(f"\nAccuracy: small.en={en_correct}/{len(results)}  Indian={in_correct}/{len(results)}")
print(f"Avg latency: small.en={np.mean([r['lat_en'] for r in results]):.0f}ms  Indian={np.mean([r['lat_indian'] for r in results]):.0f}ms")
