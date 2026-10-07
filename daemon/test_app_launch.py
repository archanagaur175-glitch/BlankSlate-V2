import asyncio, websockets, json

async def test():
    async with websockets.connect('ws://127.0.0.1:8765') as ws:
        msg = json.dumps({'type': 'send_text_message', 'text': 'BlankSlate, open notepad'})
        await ws.send(msg)
        
        while True:
            resp = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(resp)
            t = data['type']
            text = data.get('text', data.get('detail', ''))
            print(f'{t}: {text}')
            if t in ('text_reply', 'assistant_response', 'error'):
                break

asyncio.run(test())
