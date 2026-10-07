import os
import time
import sys

# Add CUDA DLL path
cuda_path = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
if cuda_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = cuda_path + ";" + os.environ.get("PATH", "")

from faster_whisper import WhisperModel

# Load Indian English model
model = WhisperModel("models/whisper-small-indian-accent-ct2", device="cuda", compute_type="float16")

# Test clips
test_dir = "test_audio_accent_v2"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

print("Testing Indian English model on accent clips:")
print("=" * 60)

for clip in clips:
    path = os.path.join(test_dir, clip)
    start = time.time()
    segments, info = model.transcribe(path, language="en", beam_size=5)
    text = " ".join([s.text for s in segments]).strip()
    latency = (time.time() - start) * 1000
    print(f'{clip}: "{text}" ({latency:.0f}ms)')

print("=" * 60)
