#!/usr/bin/env python3
"""
Universal PDF -> structured Markdown converter.

Uses pymupdf4llm for layout-aware extraction, then applies a post-processing
pass that:

  1. Detects and strips running headers/footers by FREQUENCY (a short line that
     repeats across many pages is chrome, not content) rather than by fixed regex.
  2. Removes standalone page numbers.
  3. Rejoins sentences/paragraphs that were artificially split by a page break,
     using a heuristic (previous line ends mid-sentence) so real paragraph
     boundaries, lists, and headings are preserved.
  4. Normalises book headings: "Part N ..." -> "## ", and chapters written either
     as "Chapter N ..." or as a bare numbered title "N. Title" -> "### ".

It is NOT specific to any single book. Point it at any text-based PDF.

Usage:
    python pdf_to_markdown.py input.pdf
    python pdf_to_markdown.py input.pdf -o output.md
    python pdf_to_markdown.py input.pdf --no-clean      # raw extraction only

Install:
    pip install pymupdf4llm
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf4llm

# A line is "page-number-like" if it is only digits, optionally roman numerals,
# or a "Page 12" style marker.
PAGE_NUM_RE = re.compile(
    r"^\s*(?:page\s+)?(?:\d{1,4}|[ivxlcdm]{1,7})\s*$",
    re.IGNORECASE,
)

# Heading patterns. Captures the visible title text for re-emission.
PART_RE = re.compile(r"^\s*(part\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten))\b\.?\s*(.*)$", re.IGNORECASE)
CHAPTER_RE = re.compile(r"^\s*(chapter\s+(?:\d+|[ivxlcdm]+))\b\.?\s*(.*)$", re.IGNORECASE)
# Bare numbered chapter title, e.g. "1. The Characters of the Story"
NUMBERED_TITLE_RE = re.compile(r"^\s*(\d{1,3})\.\s+([A-Z][^\n]{2,80})$")

# Sentence is considered "complete" if it ends with terminal punctuation
# (optionally followed by a closing quote/bracket).
SENTENCE_END_RE = re.compile(r"[.!?:;][\"'”’\)\]]*$")


def extract_pages(pdf_path: Path) -> list[str]:
    """Layout-aware extraction with pymupdf4llm, as real per-page chunks.

    page_chunks=True returns one dict per page (with a 'text' key), which gives
    reliable page boundaries — pymupdf4llm does NOT insert a textual page
    separator in the plain-string mode, so frequency-based header/footer
    detection needs these true boundaries.
    """
    chunks = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
    return [c.get("text", "") for c in chunks]


def _normalise_line(ln: str) -> str:
    """Strip leading markdown heading markers so chrome can be matched whether
    or not pymupdf4llm promoted it to a heading."""
    return re.sub(r"^\s*#+\s*", "", ln).strip()


def find_repeating_lines(pages: list[str], min_fraction: float = 0.5) -> set[str]:
    """
    Identify running headers/footers: short lines that appear on a large
    fraction of pages. These are page chrome, not content. Matching is done on
    the heading-stripped form, because pymupdf4llm often promotes a running
    header to a '#' heading by font size.
    """
    if len(pages) < 4:
        return set()

    counter: Counter[str] = Counter()
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        # Only the first 2 and last 2 non-empty lines can be header/footer chrome.
        candidates = lines[:2] + lines[-2:]
        for ln in set(candidates):
            norm = _normalise_line(ln)
            if norm and len(norm) <= 80:
                counter[norm] += 1

    threshold = max(3, int(len(pages) * min_fraction))
    return {norm for norm, n in counter.items() if n >= threshold}


def strip_chrome(pages: list[str], repeating: set[str]) -> str:
    """Remove repeating header/footer lines and standalone page numbers."""
    cleaned_pages: list[str] = []
    for page in pages:
        out_lines: list[str] = []
        for ln in page.splitlines():
            stripped = ln.strip()
            if _normalise_line(stripped) in repeating:
                continue
            if PAGE_NUM_RE.match(stripped):
                continue
            out_lines.append(ln)
        cleaned_pages.append("\n".join(out_lines))
    # Re-join pages with a blank line (drop the horizontal rules entirely).
    return "\n\n".join(p.strip() for p in cleaned_pages if p.strip())


def rejoin_split_paragraphs(md: str) -> str:
    """
    Rejoin lines artificially broken by page/column breaks.

    Heuristic: join line A to the following line B only when A does NOT end a
    sentence, B does not start a new block (heading, list, blank, quote), and
    B starts with a lowercase letter (a true continuation). This preserves
    intentional paragraph breaks, lists, and headings.
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        stripped = cur.strip()

        # Look ahead past a single blank line introduced by the page break.
        j = i + 1
        nxt = ""
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines):
            nxt = lines[j].strip()

        is_block = (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith(("-", "*", ">", "|", "```"))
            or re.match(r"^\d+\.\s", stripped)
        )
        next_is_block = (
            not nxt
            or nxt.startswith("#")
            or nxt.startswith(("-", "*", ">", "|", "```"))
            or re.match(r"^\d+\.\s", nxt)
        )

        if (
            stripped
            and not is_block
            and not next_is_block
            and not SENTENCE_END_RE.search(stripped)
            and nxt
            and nxt[0].islower()
        ):
            # Merge: drop the hyphen if the word was split (e.g. "exam-\nple").
            if stripped.endswith("-"):
                merged = stripped[:-1] + nxt
            else:
                merged = stripped + " " + nxt
            lines[j] = ""  # consume the continuation
            out.append(merged)
            i = j + 1
            continue

        out.append(cur)
        i += 1

    return "\n".join(out)


def normalise_headings(md: str) -> str:
    """Enforce ## for Parts and ### for Chapters (incl. bare 'N. Title')."""
    out: list[str] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        # Strip any existing markdown heading markers to re-classify cleanly.
        bare = re.sub(r"^\s*#+\s*", "", line)

        m_part = PART_RE.match(bare)
        m_chap = CHAPTER_RE.match(bare)
        m_num = NUMBERED_TITLE_RE.match(bare)

        if m_part and len(bare) <= 90:
            label, rest = m_part.group(1).strip(), m_part.group(2).strip()
            title = f"{label}: {rest}" if rest else label
            out.append(f"## {title}")
        elif m_chap and len(bare) <= 90:
            label, rest = m_chap.group(1).strip(), m_chap.group(2).strip()
            title = f"{label}: {rest}" if rest else label
            out.append(f"### {title}")
        elif m_num and len(bare) <= 90:
            num, rest = m_num.group(1), m_num.group(2).strip()
            out.append(f"### {num}. {rest}")
        else:
            out.append(line)
    return "\n".join(out)


def collapse_blank_lines(md: str) -> str:
    """Collapse 3+ consecutive blank lines down to a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


def post_process(pages: list[str]) -> str:
    """Run the full cleanup pipeline on the raw per-page extraction."""
    repeating = find_repeating_lines(pages)
    md = strip_chrome(pages, repeating)
    md = rejoin_split_paragraphs(md)
    md = normalise_headings(md)
    md = collapse_blank_lines(md)
    return md


def default_output_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(".md")


# --------------------------------------------------------------------------- #
# Folder workflow: imports/ -> exports/ -> completed/
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
IMPORTS_DIR = SCRIPT_DIR / "imports"
EXPORTS_DIR = SCRIPT_DIR / "exports"
COMPLETED_DIR = SCRIPT_DIR / "completed"


def versioned_path(target: Path) -> Path:
    """Return a non-colliding path: foo.ext, then foo_1.ext, foo_2.ext, ..."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def convert_pdf(pdf_path: Path, out_path: Path, clean: bool = True) -> bool:
    """Extract + (optionally) clean one PDF to out_path.

    Returns True on success, False if the PDF yielded no text (scanned/image).
    """
    print(f"  extracting (layout-aware): {pdf_path.name} ...", file=sys.stderr)
    pages = extract_pages(pdf_path)
    raw_md = "\n\n".join(pages)
    if not raw_md.strip():
        print(
            f"  ! skipped: no text extracted from {pdf_path.name} "
            "(scanned/image-only PDF — OCR with e.g. ocrmypdf first).",
            file=sys.stderr,
        )
        return False
    final_md = raw_md if not clean else post_process(pages)
    out_path.write_text(final_md, encoding="utf-8")
    print(f"  wrote {len(final_md):,} chars -> {out_path}", file=sys.stderr)
    return True


def select_pdfs(pdfs: list[Path], convert_all: bool, name: str | None) -> list[Path]:
    """Decide which PDFs to convert: --all, --name, single auto, or a prompt."""
    if name:
        match = [p for p in pdfs if p.name == name or p.stem == name]
        if not match:
            print(f"error: no PDF named '{name}' in imports/", file=sys.stderr)
        return match
    if convert_all or len(pdfs) == 1:
        return pdfs

    # Interactive menu for multiple PDFs.
    print("\nPDFs found in imports/:")
    for i, p in enumerate(pdfs, 1):
        print(f"  [{i}] {p.name}")
    print("  [a] all")
    raw = input("Convert which? (number, comma-separated list, or 'a'): ").strip().lower()
    if raw in ("a", "all", ""):
        return pdfs
    chosen: list[Path] = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(pdfs):
            chosen.append(pdfs[int(tok) - 1])
        else:
            print(f"  (ignoring invalid selection: {tok!r})", file=sys.stderr)
    return chosen


def run_folder_workflow(convert_all: bool, name: str | None, clean: bool) -> int:
    for d in (IMPORTS_DIR, EXPORTS_DIR, COMPLETED_DIR):
        d.mkdir(exist_ok=True)

    pdfs = sorted(p for p in IMPORTS_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {IMPORTS_DIR}. Drop one or more PDFs there and re-run.")
        return 0

    selected = select_pdfs(pdfs, convert_all, name)
    if not selected:
        print("Nothing selected. Exiting.")
        return 0

    ok = 0
    for pdf in selected:
        print(f"\n> {pdf.name}")
        out_path = versioned_path(EXPORTS_DIR / f"{pdf.stem}.md")
        if convert_pdf(pdf, out_path, clean=clean):
            dest = versioned_path(COMPLETED_DIR / pdf.name)
            pdf.rename(dest)  # move original only after a successful conversion
            print(f"  moved original -> completed/{dest.name}", file=sys.stderr)
            ok += 1
        else:
            print("  left in imports/ (not moved).", file=sys.stderr)

    print(f"\nDone: {ok}/{len(selected)} converted.")
    return 0 if ok == len(selected) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert any text-based PDF into structured Markdown. "
        "With no PDF argument, runs the imports/ -> exports/ -> completed/ workflow.",
    )
    parser.add_argument(
        "pdf", type=Path, nargs="?", default=None,
        help="Optional explicit PDF path. If omitted, the imports/ folder is used.",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .md path for explicit single-file mode (default: same name).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Folder mode: convert every PDF in imports/ without prompting.",
    )
    parser.add_argument(
        "--name", type=str, default=None,
        help="Folder mode: convert only the named PDF in imports/.",
    )
    parser.add_argument(
        "--no-clean", action="store_true",
        help="Skip post-processing and write the raw extraction.",
    )
    args = parser.parse_args(argv)
    clean = not args.no_clean

    # Folder workflow when no explicit PDF path is given.
    if args.pdf is None:
        return run_folder_workflow(args.all, args.name, clean)

    # Explicit single-file mode (does not touch the imports/exports folders).
    if not args.pdf.exists():
        print(f"error: file not found: {args.pdf}", file=sys.stderr)
        return 1
    out_path = args.output or default_output_path(args.pdf)
    if convert_pdf(args.pdf, out_path, clean=clean):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
