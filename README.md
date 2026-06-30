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

### PDF → Markdown Converter — `pdf to markdown/`

Converts any text-based PDF into a clean, structured Markdown file using [`pymupdf4llm`](https://pypi.org/project/pymupdf4llm/) for layout-aware extraction, followed by a post-processing pass that:

- Strips running headers/footers by **frequency** (a short line repeating across most pages is chrome, not content) — more reliable than fixed regex
- Removes standalone page numbers (digits and roman numerals)
- Rejoins sentences/paragraphs split by page breaks, while preserving real paragraph breaks, lists, and headings (also de-hyphenates split words)
- Normalizes book headings — `## ` for Parts, `### ` for Chapters (handles both `Chapter N …` and bare numbered `N. Title` forms)

Works on any PDF, not a single book. Scanned/image-only PDFs (no text layer) are skipped with a notice — OCR them first (e.g. `ocrmypdf`).

**Install dependencies:**
```bash
pip install pymupdf4llm
```

**Folder workflow** — drop PDFs in `imports/`, run with no arguments:
```bash
cd "pdf to markdown"
python pdf_to_markdown.py
```
- 1 PDF → converts automatically
- 2+ PDFs → numbered menu to pick one, a comma-list (`1,3`), or `a` for all

`.md` output is written to `exports/`; the original is moved to `completed/` **only after a successful conversion**. Name collisions are versioned (`book.md`, `book_1.md`, …) — nothing is ever overwritten.

**Non-interactive / one-off:**
```bash
python pdf_to_markdown.py --all            # convert every PDF in imports/
python pdf_to_markdown.py --name book.pdf  # convert just that one
python pdf_to_markdown.py some/file.pdf -o out.md   # explicit, ignores the folders
```

**Structure:**
```
pdf to markdown/
  pdf_to_markdown.py   converter — folder workflow + explicit mode
  make_test_pdf.py     generates a synthetic PDF for testing
  imports/             drop your PDF(s) here
  exports/             .md output
  completed/           originals, moved here after conversion
```

> The interactive menu needs a real terminal (uses `input()`). For automated runs use `--all` or `--name`.

---

## Requirements

- Python 3.10+
- Dependencies are per-project (see each section above)
