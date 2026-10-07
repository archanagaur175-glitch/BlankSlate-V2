import os
import sys
import time
import wave
import json

from vosk import Model, KaldiRecognizer

print("Loading Vosk Indian English LARGE model...")
model = Model("models/vosk-model-en-in-0.5")
print("Model loaded.\n")

test_dir = "test_live_accent"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

print("=" * 70)
print("TESTING VOSK LARGE INDIAN ENGLISH MODEL")
print("=" * 70)
print(f"{'Clip':<25} {'Vosk Output':<45} {'Expected'}")
print("-" * 70)

results = []
for clip in clips:
    path = os.path.join(test_dir, clip)
    wf = wave.open(path, "rb")
    
    rec = KaldiRecognizer(model, wf.getframerate())
    rec.SetWords(True)
    
    start = time.time()
    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        rec.AcceptWaveform(data)
    
    result = json.loads(rec.FinalResult())
    text = result.get("text", "").strip()
    latency = (time.time() - start) * 1000
    
    wf.close()
    
    expected = clip.replace(".wav", "").replace("_", " ")[3:]
    
    results.append({
        "clip": clip,
        "vosk": text,
        "expected": expected,
        "latency": latency,
    })
    
    match = expected.lower() in text.lower() or text.lower() in expected.lower()
    status = "✓" if match else "✗"
    
    sys.stdout.buffer.write(f"{clip:<25} {text:<45} {expected} {status}\n".encode("utf-8"))
    sys.stdout.flush()

print("=" * 70)
correct = sum(1 for r in results if r['expected'].lower() in r['vosk'].lower() or r['vosk'].lower() in r['expected'].lower())
print(f"Accuracy: {correct}/{len(results)}")
print(f"Avg latency: {sum(r['latency'] for r in results)/len(results):.0f}ms")
