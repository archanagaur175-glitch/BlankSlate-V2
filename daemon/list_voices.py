"""List available edge-tts voices."""
import asyncio
import edge_tts

async def list_voices():
    voices = await edge_tts.list_voices()
    indian = [v for v in voices if 'IN' in v['Locale'] or 'india' in v['FriendlyName'].lower()]
    print('Indian English voices:')
    for v in indian:
        print(f"  {v['ShortName']}: {v['FriendlyName']}")
    print()
    print('All English voices:')
    english = [v for v in voices if v['Locale'].startswith('en-')]
    for v in english:
        print(f"  {v['ShortName']}: {v['FriendlyName']}")

asyncio.run(list_voices())
