import os
import sys
import time
import wave
import json

from vosk import Model, KaldiRecognizer

# Load Vosk model
print("Loading Vosk Indian English model...")
model = Model("models/vosk-model-small-en-in-0.4")
print("Model loaded.\n")

# Test on recorded clips
test_dir = "test_live_accent"
clips = sorted([f for f in os.listdir(test_dir) if f.endswith(".wav")])

print("=" * 70)
print("TESTING VOSK INDIAN ENGLISH MODEL ON LIVE VOICE CLIPS")
print("=" * 70)
print(f"{'Clip':<25} {'Vosk Output':<45} {'Expected'}")
print("-" * 70)

results = []
for clip in clips:
    path = os.path.join(test_dir, clip)
    
    # Read WAV file
    wf = wave.open(path, "rb")
    
    # Create recognizer
    rec = KaldiRecognizer(model, wf.getframerate())
    rec.SetWords(True)
    
    # Process audio
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
    
    # Expected text from filename
    expected = clip.replace(".wav", "").replace("_", " ")[3:]  # Remove "cmd_XX " prefix
    
    results.append({
        "clip": clip,
        "vosk": text,
        "expected": expected,
        "latency": latency,
    })
    
    # Check match
    match = expected.lower() in text.lower() or text.lower() in expected.lower()
    status = "✓" if match else "✗"
    
    sys.stdout.buffer.write(f"{clip:<25} {text:<45} {expected} {status}\n".encode("utf-8"))
    sys.stdout.flush()

print("=" * 70)
print(f"\nAvg latency: {sum(r['latency'] for r in results)/len(results):.0f}ms")
