import sys, os, subprocess, time

log = r"C:\Users\archa\Documents\BlankSlate_V2\daemon\live_output.log"
env = os.environ.copy()
env["BS_STT_DEVICE"] = "cuda"
env["BS_STT_COMPUTE_TYPE"] = "float16"

# Add Ollama CUDA DLL directory so cublas64_12.dll is found at inference time
cuda_dll = r"C:\Users\archa\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"
env["PATH"] = cuda_dll + ";" + env.get("PATH", "")

with open(log, "w") as f:
    proc = subprocess.Popen(
        [sys.executable, "-m", "blankslate"],
        cwd=r"C:\Users\archa\Documents\BlankSlate_V2\daemon",
        stdout=f,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=1,
    )
    print(f"Daemon PID: {proc.pid}")
    print(f"Log: {log}")
    
    # Wait and show log
    time.sleep(15)
    f.flush()
    
with open(log) as f:
    print(f.read())
