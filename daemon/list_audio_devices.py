"""List available audio input devices.

Usage: python list_audio_devices.py
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice not installed. Run: pip install sounddevice")
    sys.exit(1)

print("=== Audio Input Devices ===\n")
devices = sd.query_devices()
for i, d in enumerate(devices):
    if d["max_input_channels"] > 0:
        default = " (DEFAULT)" if i == sd.default.device[0] else ""
        print(f"  [{i}] {d['name']}")
        print(f"      channels={d['max_input_channels']}, rate={d['default_samplerate']:.0f}Hz{default}")
        print()

print(f"Current default input: [{sd.default.device[0]}] {devices[sd.default.device[0]]['name']}")
print(f"\nTo select a device, set: BS_MIC_DEVICE=<index>")
print(f"Example: $env:BS_MIC_DEVICE='2'")
