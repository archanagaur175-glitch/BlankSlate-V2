# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

_pkgs = [
    'faster_whisper',
    'sounddevice',
    'pynput',
    'webrtcvad',
    'ctranslate2',
    'pocket_tts',
    'sentencepiece',
    'soundfile',
    'comtypes',
    'pycaw',
    'mss',
    'watchdog',
    'sqlite_vec',
    'ddgs',
    'pynvml',
    'rapidocr_onnxruntime',
    'cv2',
    'httpx',
]

_extra_datas = []
_extra_binaries = []
_extra_hiddenimports = []

for _pkg in _pkgs:
    _datas, _binaries, _hidden = collect_all(_pkg)
    _extra_datas += _datas
    _extra_binaries += _binaries
    _extra_hiddenimports += _hidden

hiddenimports = [
    'numpy',
    'pynput.keyboard',
    'pynput.keyboard._win32',
    'win32gui',
    'win32process',
    'win32clipboard',
    'win32crypt',
    'win32api',
    'win32con',
    'win32evtlog',
    'pywintypes',
    'pythoncom',
    'faster_whisper',
    'sounddevice',
    'pynput',
    'webrtcvad',
    'ctranslate2',
    'pocket_tts',
    'sentencepiece',
    'soundfile',
    'comtypes',
    'pycaw',
    'mss',
    'watchdog',
    'sqlite_vec',
    'ddgs',
    'pynvml',
    'rapidocr_onnxruntime',
    'cv2',
    'httpx',
] + _extra_hiddenimports

a = Analysis(
    ['run_daemon.py'],
    pathex=[],
    binaries=_extra_binaries,
    datas=[
        ('blankslate', 'blankslate'),
        ('voice_ref_friday_clean.wav', '.'),
    ] + _extra_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'nltk',
        'sklearn',
        'pandas',
        'spacy',
        'jieba',
        'sudachipy',
        'sudachidict_core',
        'stanza',
        'blingfire',
        'pysbd',
        'syntok',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='blankslate-daemon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='blankslate-daemon',
)
