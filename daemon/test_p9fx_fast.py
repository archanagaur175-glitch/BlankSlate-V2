import asyncio, websockets, json, sys, io, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


async def send(ws, text, timeout=120):
    t0 = time.perf_counter()
    await ws.send(json.dumps({"type": "send_text_message", "text": text}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") in ("text_reply", "error"):
            return m, time.perf_counter() - t0


async def main():
    for a in range(12):
        try:
            async with websockets.connect("ws://127.0.0.1:8765") as ws:
                await ws.send(json.dumps({"type": "get_state"}))
                await ws.recv()
                break
        except Exception:
            await asyncio.sleep(5)

    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        for q in ["calculate 6 times 7", "hello there, how are you", "what is the current time", "search online for news"]:
            m, dt = await send(ws, q)
            print(f"[{dt:6.2f}s intent={m.get('intent')} tools={m.get('tools')}] {q!r}")
            print(f"   reply={repr(m.get('text'))[:120]}")

asyncio.run(main())
