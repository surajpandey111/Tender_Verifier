# Building & Sharing as a .exe

## The core fact

`pyinstaller` bundles **Python itself + every pip package + your code**
into one folder/exe. That solves "does the other person need Python
installed" — they don't. It does **NOT** solve two other things, which
this guide covers:

1. **Tesseract OCR** is a separate program (not a Python package) — it has
   to travel with the exe some other way.
2. **The Groq API key** is a secret — it must never be baked into the exe.

## What you build (on YOUR Windows machine — you're already set up)

```powershell
pip install pyinstaller
pyinstaller build_exe.spec
```

This produces `dist\TenderVerifier\` — a **folder**, not a single file
(this project uses a "onedir" build on purpose, not "onefile" — see
*Why onedir, not onefile* below). Inside:

```
dist/TenderVerifier/
    TenderVerifier.exe        <- what the user double-clicks / runs
    _internal/                <- Python runtime + all pip packages + config/*.json (bundled automatically)
```

This exact spec file (`build_exe.spec`) was tested end-to-end in this
project — including the two real bugs it caught:
- The SQLite database path broke inside a frozen build because
  `Path(__file__)` doesn't resolve to a real folder for bundled Python code
  — fixed via `core/paths.py`, which now correctly separates "writable
  data next to the exe" (the DB) from "read-only bundled data" (config json,
  which PyInstaller actually extracts into `_internal/`, not next to the exe).
- Multiprocessing needs `multiprocessing.freeze_support()` when frozen, or
  the worker processes fail — already added to `main.py`.

Both are already fixed in the code you have; you don't need to touch this.

## What the RECIPIENT needs — the actual dependency list

| Dependency | Do they need to install it separately? |
|---|---|
| Python | **No** — bundled inside `_internal/` by PyInstaller |
| pip packages (opencv, pymupdf, etc.) | **No** — bundled |
| Your code | **No** — bundled (and not readable as plain `.py` source anymore) |
| **Tesseract OCR** | **Yes, unless you bundle it too** — see below |
| **Groq API key** | **Yes, their own `.env` file** — see below |

### Handling Tesseract for the recipient (pick one)

**Option A — bundle a portable Tesseract (recommended, zero-setup for them)**
1. Download the portable/standalone Tesseract build for Windows (search
   "tesseract portable windows" or extract the `Tesseract-OCR` folder from
   a normal installer without running the installer's system-wide setup).
2. Copy that whole `Tesseract-OCR` folder into `dist/TenderVerifier/`, so
   you end up with:
   ```
   dist/TenderVerifier/
       TenderVerifier.exe
       _internal/
       Tesseract-OCR/
           tesseract.exe
           tessdata/
   ```
3. `ocr/preprocess.py` already auto-detects this folder and points
   `pytesseract` at it — **no environment variable or code change needed
   on the recipient's machine.**
4. Zip the whole `dist/TenderVerifier/` folder and send that.

**Option B — recipient installs Tesseract themselves**
Simpler for you, one extra step for them: they run the normal Tesseract
Windows installer (link is in `check_setup.py`'s error message) before
running your exe. Fine for internal team use where you can just tell them
to do this once.

### Handling the Groq key for the recipient

Never put a real key inside the exe or its bundled files — anyone can
extract strings from a PyInstaller bundle with basic tools, so it's not
actually hidden, and you'd be handing out your own quota/secret.

Instead: ship a `.env.example` alongside the exe folder, and tell the
recipient to do what you just did:
```
copy .env.example .env
notepad .env      (paste their own free key from console.groq.com)
```
`main.py` loads `.env` from the current folder automatically — same
mechanism as running from source, no extra step for a frozen build.

If it's just for your own team and you're fine with everyone sharing your
quota, you can ship your real filled-in `.env` instead — your call, your risk.

## Final distribution package

Zip this whole folder and send it:
```
TenderVerifier_v1/
    TenderVerifier.exe
    _internal/                 (from the build — don't touch)
    Tesseract-OCR/              (optional, Option A above)
    .env.example
    config/                     (only needed if you want them to edit
                                 document_rules.json — otherwise it's
                                 already bundled inside _internal/)
```

Recipient's setup, start to finish:
```
1. Unzip the folder anywhere
2. copy .env.example .env, then paste their Groq key in .env
3. Make a tenders_root folder with their tender subfolders + PDFs
4. Open a terminal in that folder, run:
   TenderVerifier.exe --tenders-root tenders_root --workers 4
```
That's it — no Python, no pip install, no separate Tesseract install if
you did Option A.

## Why onedir, not onefile

PyInstaller can also build a single `.exe` with everything crammed inside
(`--onefile`). It's tempting for "just one file to share," but:
- It self-extracts to a temp folder on every single run, which is slow
  for a tool meant to process hundreds of PDFs in one sitting.
- It makes the Tesseract-bundling trick above harder (nothing sits in a
  stable folder next to the exe).
The onedir folder above is barely more effort to share (zip the folder,
same as zipping one file) and avoids both problems.

## Rebuilding after you change the code

Just rerun:
```powershell
rmdir /s /q build dist
pyinstaller build_exe.spec
```
Any time you edit `config/document_rules.json` or `.py` files, this picks
up the changes automatically — the spec file already points at your
source files, nothing else to update.
