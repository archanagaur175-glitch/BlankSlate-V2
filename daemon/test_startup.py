"""Phase 4: End-to-end verification — daemon startup and latency test."""
import os
import sys
import io
import time
import subprocess
import psutil

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Add Ollama's CUDA directory
cuda_path = r'C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12'
os.add_dll_directory(cuda_path)
os.environ['PATH'] = cuda_path + ';' + os.environ['PATH']


def measure_startup():
    """Measure daemon startup time and memory usage."""
    print("=" * 70)
    print("PHASE 4: DAEMON STARTUP VERIFICATION")
    print("=" * 70)

    # Kill any existing daemon
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if proc.info['name'] == 'python.exe' and proc.info['cmdline']:
                cmdline = ' '.join(proc.info['cmdline'])
                if 'blankslate' in cmdline.lower():
                    proc.kill()
                    print(f"Killed existing daemon: PID {proc.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(1)

    # Measure baseline memory
    process = psutil.Process()
    baseline_memory = process.memory_info().rss / 1024 / 1024  # MB
    print(f"\nBaseline memory: {baseline_memory:.1f} MB")

    # Start daemon
    print("\nStarting daemon...")
    daemon_dir = os.path.join(os.path.dirname(__file__), "blankslate")
    start_time = time.time()

    proc = subprocess.Popen(
        [sys.executable, "-m", "blankslate"],
        cwd=os.path.dirname(__file__),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "BS_STT_DEVICE": "cuda", "BS_STT_COMPUTE_TYPE": "float16"},
    )

    # Wait for daemon to start
    startup_time = None
    for i in range(30):  # 30 second timeout
        time.sleep(1)
        if proc.poll() is not None:
            print(f"Daemon exited with code: {proc.returncode}")
            stderr = proc.stderr.read().decode()
            print(f"Stderr: {stderr[:500]}")
            return False

        # Check if IPC is listening
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(('127.0.0.1', 8765))
            sock.close()
            if result == 0:
                startup_time = time.time() - start_time
                print(f"IPC listening after {startup_time:.2f}s")
                break
        except Exception:
            pass

    if startup_time is None:
        print("ERROR: Daemon did not start within 30 seconds")
        proc.kill()
        return False

    # Measure memory after startup
    time.sleep(2)  # Let STT model load
    try:
        daemon_proc = psutil.Process(proc.pid)
        memory_after = daemon_proc.memory_info().rss / 1024 / 1024  # MB
        print(f"Memory after startup: {memory_after:.1f} MB")
        print(f"Memory delta: {memory_after - baseline_memory:.1f} MB")
    except psutil.NoSuchProcess:
        print("Could not measure daemon memory")

    # Test STT model loading
    print("\n--- STT Model Test ---")
    from blankslate.voice.stt import STTEngine
    stt_start = time.time()
    stt = STTEngine(
        model_name="small.en",
        device="cuda",
        compute_type="float16",
        vocabulary_prompt="BlankSlate, Chrome, notepad",
    )
    stt_load_time = time.time() - stt_start
    print(f"STT load time: {stt_load_time:.2f}s")
    print(f"STT ready: {stt.is_ready}")
    print(f"STT device: {stt._device}")

    # Test transcription latency
    if stt.is_ready:
        print("\n--- Transcription Latency Test ---")
        import numpy as np
        # Generate 3 seconds of synthetic audio
        audio = np.random.randn(16000 * 3).astype(np.float32) * 0.1
        audio_bytes = (audio * 32768).astype(np.int16).tobytes()

        times = []
        for i in range(3):
            t0 = time.time()
            result = stt.transcribe(audio_bytes)
            latency = (time.time() - t0) * 1000
            times.append(latency)
            print(f"  Run {i+1}: {latency:.0f}ms")

        avg_latency = sum(times) / len(times)
        print(f"  Average: {avg_latency:.0f}ms")

    # Cleanup
    print("\n--- Cleanup ---")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    print("\n" + "=" * 70)
    print("STARTUP VERIFICATION SUMMARY")
    print("=" * 70)
    print(f"Startup time: {startup_time:.2f}s")
    print(f"STT load time: {stt_load_time:.2f}s")
    print(f"STT device: {stt._device}")
    if stt.is_ready:
        print(f"Transcription avg: {avg_latency:.0f}ms")
    print("=" * 70)

    return True


if __name__ == "__main__":
    success = measure_startup()
    sys.exit(0 if success else 1)
