"""Quick benchmark: medium.en on GPU."""
import os
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']

from faster_whisper import WhisperModel

test_dir = os.path.join(os.path.dirname(__file__), "test_audio")
clips = [
    ("01_wake_word.wav", "Blank Slate"),
    ("02_wake_command.wav", "Blank Slate, open notepad"),
    ("04_switch_window.wav", "Switch to Chrome"),
    ("08_background_noise.wav", "Blank Slate, what is the weather like today"),
    ("10_adversarial.wav", "Hey Blank Slate, can you open the notepad application please"),
]

print("Loading medium.en...")
model = WhisperModel("medium.en", device="cuda", compute_type="float16")

results = []
for fn, exp in clips:
    fp = os.path.join(test_dir, fn)
    start = time.time()
    segs, info = model.transcribe(fp, language="en", initial_prompt="BlankSlate")
    segs = list(segs)
    lat = time.time() - start
    text = " ".join(s.text.strip() for s in segs)
    exp_l = exp.lower()
    txt_l = text.lower()
    words = exp_l.split()
    acc = sum(1 for w in words if w in txt_l) / len(words)
    bs = "blankslate" in txt_l.replace(" ", "") or "blank slate" in txt_l
    status = "Y" if bs else "N"
    print(f"  {fn}: {lat:.3f}s | {text!r} | acc={acc:.0%} | bs={status}")
    results.append((lat, acc, bs))

avg_l = sum(r[0] for r in results) / len(results)
avg_a = sum(r[1] for r in results) / len(results)
bs_p = sum(1 for r in results if r[2])
print(f"  AVG: {avg_l:.3f}s, {avg_a:.0%}, BS={bs_p}/5")
