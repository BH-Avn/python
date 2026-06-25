# Python Projects

A collection of Python tools built for personal use.

> **Note:** The code in this repo was written by Claude Opus. I cannot guarantee efficiency, but I can guarantee it works.

---

## Projects

### Novel Scraper Suite — `scraper/`

A CLI tool for scraping web novels from multiple sites and saving them as `.txt` or `.epub` files.

**Supported sites:** Novelbin · Novelarrow · 9kafe · Generic (any site with a TOC or next-chapter links)

**Modes:**
- **Interactive** — menu-driven, no arguments needed
- **CLI** — fully scriptable with flags
- **Batch** — unattended, runs a list of novels from a JSON file

**Install dependencies:**
```bash
pip install playwright playwright-stealth requests ebooklib beautifulsoup4
playwright install chromium
```

**Run (interactive):**
```bash
cd scraper
python main.py
```

**Run (CLI examples):**
```bash
python main.py novelbin   --url URL --name "Novel Name" --range 1-200 --workers 4 --epub
python main.py novelarrow --url URL --name "Novel Name" --workers 3 --epub
python main.py kafe9      --url URL --name "Novel Name"
python main.py generic    --url URL --name "Novel Name" --mode toc --workers 4 --epub
python main.py convert    --dir ./output
```

**Batch mode** (`novels.json`):
```json
[
  {"site": "novelbin",   "url": "...", "name": "...", "range": "1-500", "workers": 4, "epub": true},
  {"site": "novelarrow", "url": "...", "name": "...", "workers": 3},
  {"site": "generic",    "url": "...", "name": "...", "mode": "toc",   "workers": 4, "epub": true}
]
```
```bash
python main.py --batch novels.json
```

**Structure:**
```
scraper/
  main.py          entry point — interactive + CLI + batch
  lastUrl.py       dev utility: dumps chapter URLs from a novel page
  core/
    browser.py     Playwright browser setup
    cache.py       chapter URL caching
    epub_writer.py .txt → .epub converter
    scraper.py     base scraping logic
  sites/
    novelbin.py
    novelarrow.py
    kafe9.py
    generic.py
```

Output is written to `scraper/output/<novel-name>/`. EPUBs land alongside the `.txt` file.

---

### Anki Card Generator — `anki-generation/`

Reads a formatted `cards.txt` file and pushes flashcards directly into Anki via [AnkiConnect](https://ankiweb.net/shared/info/2055492159). Supports Basic and Cloze card types. Clears the file after a successful run.

**Requirements:** Anki must be open with the AnkiConnect add-on installed.

**Install dependencies:**
```bash
pip install requests
```

**Run:**
```bash
cd anki-generation
python generation.py
```

**`cards.txt` format:**
```
Deck: My Deck  Card Type: Basic
Front: What is the powerhouse of the cell?
Back: The mitochondria

Deck: My Deck  Card Type: Basic
Front: What does HTTP stand for?
Back: HyperText Transfer Protocol
```

- `Card Type` is optional — defaults to `Basic`
- Use `Card Type: Cloze` for cloze-deletion cards
- Separate cards with a blank line
- The file is cleared automatically after cards are added

---

## Requirements

- Python 3.10+
- Dependencies are per-project (see each section above)
