import asyncio, websockets, json, sys, io, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


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
        t0 = time.perf_counter()
        await ws.send(json.dumps({"type": "send_text_message", "text": "hello there, how are you"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 300))
            print("msg type:", m.get("type"), "| keys:", list(m.keys()))
            if m.get("type") in ("text_reply", "error"):
                print("  DURATION %.2fs" % (time.perf_counter() - t0))
                print("  full msg:", json.dumps(m)[:300])
                break


asyncio.run(main())
