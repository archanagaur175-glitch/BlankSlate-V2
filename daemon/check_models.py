"""Check available model repos."""
from huggingface_hub import list_repo_files

repos = [
    'distil-whisper/distil-small.en',
    'distil-whisper/distil-medium.en',
    'distil-whisper/distil-large-v3',
    'openai/whisper-tiny.en',
    'openai/whisper-base.en',
    'openai/whisper-small.en',
    'openai/whisper-medium.en',
    'openai/whisper-large-v3',
]

for repo in repos:
    try:
        files = list_repo_files(repo)
        ct2 = [f for f in files if 'model.bin' in f]
        has_ct2 = 'YES' if ct2 else 'NO'
        print(f'{repo}: {len(files)} files, CT2 model: {has_ct2}')
    except Exception as e:
        print(f'{repo}: {e}')
