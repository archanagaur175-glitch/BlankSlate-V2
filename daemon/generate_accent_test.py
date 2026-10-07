"""Generate accent-stress test set using Indian English TTS."""
import asyncio
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import edge_tts

# Indian English voice
VOICE = "en-IN-NeerjaNeural"

# Test clips for Indian-accented English
ACCENT_CLIPS = [
    ("accent_01_wake_alone.wav", "Blank Slate", "Wake word alone, natural pace"),
    ("accent_02_wake_command.wav", "Blank Slate, open notepad", "Wake word + command, natural pace"),
    ("accent_03_retroflex_t.wav", "switch to Chrome and type the text", "Retroflex T/D consonant clusters"),
    ("accent_04_consonant_cluster.wav", "list all the open windows and switch between them", "Consonant clusters + proper nouns"),
    ("accent_05_app_names.wav", "open Visual Studio Code and switch to Chrome", "App name stress test"),
    ("accent_06_longer_command.wav", "Blank Slate, can you please open the notepad application and type hello world in it", "Longer natural command"),
    ("accent_07_conversational.wav", "hey Blank Slate what time is it right now", "Conversational pace"),
    ("accent_08_noise_accent.wav", "Blank Slate open the Chrome browser and search for something", "Noise + accent combination"),
    ("accent_09_proper_noun.wav", "switch to Cursor and open the terminal", "Proper noun: Cursor"),
    ("accent_10_full_command.wav", "Blank Slate, list all the open windows, then switch to notepad and type some text", "Complex multi-step command"),
]


async def generate_accent_clips():
    """Generate test clips using Indian English TTS."""
    test_dir = os.path.join(os.path.dirname(__file__), "test_audio_accent")
    os.makedirs(test_dir, exist_ok=True)

    print(f"Generating {len(ACCENT_CLIPS)} accent test clips using {VOICE}...")
    print(f"Output directory: {test_dir}")

    for filename, text, desc in ACCENT_CLIPS:
        filepath = os.path.join(test_dir, filename)
        try:
            communicate = edge_tts.Communicate(text, VOICE)
            await communicate.save(filepath)
            size = os.path.getsize(filepath)
            print(f"  ✅ {filename}: {desc} ({size} bytes)")
        except Exception as e:
            print(f"  ❌ {filename}: FAILED - {e}")
            return False

    print(f"\nAccent test set generated: {len(ACCENT_CLIPS)} clips")
    return True


if __name__ == "__main__":
    asyncio.run(generate_accent_clips())
