"""Generate Indian English accent test clips using edge-tts."""
import asyncio
import edge_tts
import os
import io
import subprocess
import imageio_ffmpeg

VOICE = "en-IN-NeerjaNeural"  # Indian English female voice
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_audio_accent_v2")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Diverse test phrases — not just wake words
PHRASES = [
    # App launch commands
    ("01_open_notepad", "Hey BlankSlate, open notepad"),
    ("02_open_chrome", "Blank Slate, open the browser please"),
    ("03_launch_word", "Hey assistant, open Microsoft Word"),
    ("04_start_calculator", "Blank Slate, start the calculator"),
    ("05_open_paint", "Open paint application"),

    # Window management
    ("06_switch_window", "Switch to the Chrome window"),
    ("07_list_windows", "Show me all the open windows"),
    ("08_minimize_window", "Minimize the current window"),

    # Text input
    ("09_type_hello", "Type hello world in the document"),
    ("10_dictate_sentence", "The quick brown fox jumps over the lazy dog"),

    # Time and date
    ("11_whats_time", "What is the current time"),
    ("12_whats_date", "What is today's date"),

    # Natural conversation
    ("13_good_morning", "Good morning, how are you doing today"),
    ("14_weather_query", "What is the weather like in Mumbai right now"),
    ("15_reminder", "Remind me to buy groceries after work"),
    ("16_note_taking", "Take a note that the meeting is at three PM"),
    ("17_search_query", "Search the web for best Indian restaurants near me"),
    ("18_math_calc", "What is 247 multiplied by 83"),
    ("19_general_chat", "Tell me a joke about programming"),
    ("20_complex_command", "Open notepad and type my daily report for today"),

    # Technical terms
    ("21_pronunciation_hard", "Launch Visual Studio Code"),
    ("22_numbers", "Set a timer for fifteen minutes and thirty seconds"),
    ("23_email", "Open my email and check for new messages"),
    ("24_file_management", "Open file manager and navigate to downloads folder"),
    ("25_system", "What is the battery percentage on my laptop"),
]

async def generate():
    for filename, text in PHRASES:
        output = os.path.join(OUTPUT_DIR, f"{filename}.wav")
        communicate = edge_tts.Communicate(text, VOICE)
        # Collect MP3 chunks from stream
        mp3_data = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data.write(chunk["data"])
        mp3_data.seek(0)
        # Convert MP3 to WAV using ffmpeg
        proc = subprocess.Popen(
            [FFMPEG, "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        wav_data, _ = proc.communicate(input=mp3_data.read())
        with open(output, "wb") as f:
            f.write(wav_data)
        print(f"Generated: {filename}.wav ({len(text)} chars)")

asyncio.run(generate())
print(f"\nDone! {len(PHRASES)} clips in {OUTPUT_DIR}")
