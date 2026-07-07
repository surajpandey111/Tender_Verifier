# PyInstaller build spec for Tender Verifier.
# Build with:  pyinstaller build_exe.spec
# Produces:    dist/TenderVerifier/TenderVerifier.exe  (folder build — see BUILD_EXE.md for why)

import sys
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH)

a = Analysis(
    ['main.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        # PyInstaller only bundles Python code by default — config JSON must be
        # explicitly included, or the recipient's exe will crash looking for it.
        (str(project_root / 'config'), 'config'),
    ],
    hiddenimports=[
        'groq',
        'dotenv',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TenderVerifier',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # keep True — this is a CLI batch tool, users need to see progress/errors
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='TenderVerifier',
)
