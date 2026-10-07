import asyncio, websockets, json

async def test(query):
    async with websockets.connect('ws://127.0.0.1:8765') as ws:
        msg = json.dumps({'type': 'send_text_message', 'text': query})
        await ws.send(msg)
        
        while True:
            resp = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(resp)
            t = data['type']
            text = data.get('text', data.get('detail', ''))
            print(f'  {t}: {text}')
            if t in ('text_reply', 'assistant_response', 'error'):
                break

async def main():
    tests = [
        ('BlankSlate, open word', 'Word (Win32)'),
        ('BlankSlate, launch powerpoint', 'PowerPoint (Win32)'),
        ('BlankSlate, start calculator', 'Calculator (UWP)'),
        ('BlankSlate, open paint', 'Paint (UWP)'),
        ('BlankSlate, launch email', 'Outlook (Win32)'),
        ('BlankSlate, open the camera', 'Camera (UWP)'),
    ]
    for q, label in tests:
        print(f'\n>>> {label}: {q}')
        await test(q)

asyncio.run(main())
