"""
main.py — Novel Scraper Suite entry point.

Interactive mode (no args):
    python main.py

CLI mode (non-interactive, scriptable):
    python main.py novelbin   --url URL --name NAME [--range 1-100] [--workers 3] [--epub]
    python main.py novelarrow --url URL --name NAME [--range 1-100] [--workers 3] [--epub]
                                                    [--output-dir PATH]
    python main.py kafe9      --url URL --name NAME
    python main.py generic    --url URL --name NAME --mode toc|next
                                                    [--range 1-100] [--workers 3]
                                                    [--delay 0.5]   [--epub]
    python main.py convert    --dir PATH

Batch mode (run multiple novels unattended):
    python main.py --batch novels.json

    novels.json format:
    [
      {"site": "novelbin",   "url": "...", "name": "...", "range": "1-200",
       "workers": 3, "epub": true},
      {"site": "novelarrow", "url": "...", "name": "...", "workers": 5},
      {"site": "generic",    "url": "...", "name": "...", "mode": "toc",
       "workers": 4, "delay": 0.5, "epub": true},
      {"site": "kafe9",      "url": "...", "name": "..."}
    ]
"""
import argparse
import importlib
import json
import os
import sys
import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

BANNER = """
╔══════════════════════════════════════════╗
║       Novel Scraper Suite  v2.0          ║
╠══════════════════════════════════════════╣
║  [1]  Novelbin   — scrape to .txt/.epub  ║
║  [2]  Novelarrow — scrape to .txt/.epub  ║
║  [3]  9kafe      — download EPUBs        ║
║  [4]  Convert    — .txt → .epub          ║
║  [5]  Generic    — any website           ║
║  [6]  NovelLive  — Cloudflare-protected  ║
║  [0]  Exit                               ║
╚══════════════════════════════════════════╝
"""

ROUTES = {
    "1": ("sites.novelbin",   "Novelbin"),
    "2": ("sites.novelarrow", "Novelarrow"),
    "3": ("sites.kafe9",      "9kafe"),
    "5": ("sites.generic",    "Generic"),
    "6": ("sites.novellive",  "NovelLive"),
}


# ── Inline converter ──────────────────────────────────────────────────────────

def _run_converter(target_dir: str = ""):
    from core.epub_writer import txt_to_epub

    if not target_dir:
        target_dir = input(
            "Enter directory path (press Enter for current directory): "
        ).strip() or os.getcwd()

    if not os.path.isdir(target_dir):
        print(f"Error: '{target_dir}' does not exist.")
        return

    txt_files = [f for f in os.listdir(target_dir) if f.lower().endswith(".txt")]
    if not txt_files:
        print(f"No .txt files found in '{target_dir}'.")
        return

    if target_dir:
        # CLI / batch mode — convert all files without prompting
        files_to_convert = txt_files
    else:
        print(f"\nFound {len(txt_files)} file(s) in {target_dir}:")
        for i, fn in enumerate(txt_files, 1):
            print(f"  [{i}] {fn}")
        choice = input("\nEnter number to convert (or 'all'): ").strip().lower()
        files_to_convert = []
        if choice == "all":
            files_to_convert = txt_files
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(txt_files):
                    files_to_convert.append(txt_files[idx])
                else:
                    print("Invalid number.")
                    return
            except ValueError:
                print("Invalid input.")
                return

    for txt_file in files_to_convert:
        in_path = os.path.join(target_dir, txt_file)
        out_path = os.path.join(target_dir, txt_file.rsplit(".", 1)[0] + ".epub")
        print(f"\nConverting '{txt_file}'...")
        try:
            txt_to_epub(in_path, out_path)
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\nConversion complete.")


# ── CLI argument parser ───────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Novel Scraper Suite — scrape novels to .txt/.epub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py novelbin --url https://... --name MyNovel --range 1-500 --workers 4 --epub\n"
            "  python main.py generic  --url https://... --name MyNovel --mode toc --workers 5\n"
            "  python main.py --batch novels.json\n"
            "  python main.py convert  --dir C:/Books\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="site")

    # ── novelbin ──────────────────────────────────────────────────────────────
    nb = subparsers.add_parser("novelbin", help="Scrape novelbin.com")
    nb.add_argument("--url",     required=True, help="Chapter URL (used to find dropdown)")
    nb.add_argument("--name",    required=True, help="Novel name (folder + file)")
    nb.add_argument("--range",   dest="chapter_range", metavar="START-END",
                    help="Chapter range, e.g. 1-100")
    nb.add_argument("--workers", type=int, default=3,
                    help="Parallel workers (default 3; use 1 for sequential)")
    nb.add_argument("--delay",   type=float, default=0.5,
                    help="Per-worker delay in seconds (default 0.5)")
    nb.add_argument("--epub",    action="store_true", help="Convert to EPUB after scraping")

    # ── novelarrow ────────────────────────────────────────────────────────────
    na = subparsers.add_parser("novelarrow", help="Scrape novelarrow.com")
    na.add_argument("--url",        required=True, help="Contents page URL")
    na.add_argument("--name",       required=True, help="Novel name (folder + file)")
    na.add_argument("--range",      dest="chapter_range", metavar="START-END")
    na.add_argument("--workers",    type=int, default=3)
    na.add_argument("--delay",      type=float, default=0.5)
    na.add_argument("--epub",       action="store_true")
    na.add_argument("--output-dir", metavar="DIR",
                    help="Override default output/ folder")

    # ── kafe9 ─────────────────────────────────────────────────────────────────
    k = subparsers.add_parser("kafe9", help="Download EPUBs from 9kafe.com")
    k.add_argument("--url",  required=True, help="Novel page URL")
    k.add_argument("--name", required=True, help="Folder name for downloads")

    # ── generic ───────────────────────────────────────────────────────────────
    g = subparsers.add_parser("generic", help="Generic scraper — works on most sites")
    g.add_argument("--url",     required=True)
    g.add_argument("--name",    required=True)
    g.add_argument("--mode",    choices=["toc", "next"], default="toc",
                    help="toc = table-of-contents page; next = follow next-chapter links")
    g.add_argument("--range",   dest="chapter_range", metavar="START-END")
    g.add_argument("--workers", type=int, default=3,
                    help="Parallel workers (TOC mode only; ignored for next-links mode)")
    g.add_argument("--delay",   type=float, default=0.5)
    g.add_argument("--epub",    action="store_true")

    # ── novellive ─────────────────────────────────────────────────────────────
    nl = subparsers.add_parser(
        "novellive", help="Scrape novellive.app (Cloudflare-protected, patchright)"
    )
    nl.add_argument("--url",      required=True,
                    help="next mode: a chapter URL to start from; toc mode: the book URL")
    nl.add_argument("--name",     required=True, help="Novel name (folder + file)")
    nl.add_argument("--mode",     choices=["next", "toc"], default="next",
                    help="next = follow next-chapter links (sequential); "
                         "toc = harvest book page then scrape in parallel")
    nl.add_argument("--range",    dest="chapter_range", metavar="START-END",
                    help="toc mode only — chapter range, e.g. 1-200")
    nl.add_argument("--workers",  type=int, default=3,
                    help="toc mode only — parallel workers (default 3; keep ≤5)")
    nl.add_argument("--delay",    type=float, default=0.5,
                    help="Delay between chapters per worker (default 0.5)")
    nl.add_argument("--max",      dest="max_chapters", type=int, default=5000,
                    help="next mode only — stop after this many chapters (default 5000)")
    nl.add_argument("--epub",     action="store_true", help="Convert to EPUB after scraping")
    nl.add_argument("--headless", action="store_true",
                    help="Run the browser headless (less reliable against Cloudflare)")

    # ── convert ───────────────────────────────────────────────────────────────
    c = subparsers.add_parser("convert", help="Batch convert .txt files to .epub")
    c.add_argument("--dir", default="", metavar="DIR",
                   help="Directory containing .txt files (default: current dir)")

    # ── batch ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--batch", metavar="FILE",
        help="JSON batch file — run multiple novels unattended",
    )

    return parser


# ── CLI dispatcher ────────────────────────────────────────────────────────────

def _dict_to_namespace(d: dict) -> argparse.Namespace:
    """Converts a batch JSON entry dict to an argparse-compatible Namespace."""
    ns = argparse.Namespace()
    for k, v in d.items():
        # Map JSON keys to argparse dests: 'range' → 'chapter_range', 'max' → 'max_chapters'
        if k == "range":
            key = "chapter_range"
        elif k == "max":
            key = "max_chapters"
        else:
            key = k.replace("-", "_")
        setattr(ns, key, v)
    # Fill in defaults for optional fields
    for attr, default in [
        ("workers", 3), ("delay", 0.5), ("epub", False),
        ("chapter_range", None), ("mode", "toc"), ("output_dir", None),
        ("max_chapters", 5000), ("headless", False),
    ]:
        if not hasattr(ns, attr):
            setattr(ns, attr, default)
    return ns


def _dispatch_cli(args):
    """Routes a parsed Namespace to the correct site's run_cli() function."""
    site = getattr(args, "site", None)

    if site == "convert":
        _run_converter(target_dir=args.dir)
        return

    site_map = {
        "novelbin":   "sites.novelbin",
        "novelarrow": "sites.novelarrow",
        "kafe9":      "sites.kafe9",
        "generic":    "sites.generic",
        "novellive":  "sites.novellive",
    }

    if site not in site_map:
        print(f"Unknown site '{site}'. Run with --help for usage.")
        return

    mod = importlib.import_module(site_map[site])
    mod.run_cli(args)


def _run_batch(batch_file: str):
    """Loads a JSON batch file and runs each entry in sequence."""
    if not os.path.isfile(batch_file):
        print(f"Batch file not found: {batch_file}")
        return

    with open(batch_file, "r", encoding="utf-8") as f:
        try:
            entries = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in batch file: {e}")
            return

    print(f"Batch mode — {len(entries)} novel(s) queued.\n")

    for i, entry in enumerate(entries, 1):
        site = entry.get("site", "")
        name = entry.get("name", "?")
        print(f"\n{'─' * 44}")
        print(f"[{i}/{len(entries)}] {site} — {name}")
        print(f"{'─' * 44}")
        try:
            args = _dict_to_namespace(entry)
            _dispatch_cli(args)
        except KeyboardInterrupt:
            print("\n\nBatch interrupted by user.")
            break
        except Exception as e:
            print(f"\nError processing '{name}': {e}")
            print("Continuing with next entry...")

    print("\nBatch complete.")


# ── Interactive mode ──────────────────────────────────────────────────────────

def _run_interactive():
    print(BANNER)

    while True:
        choice = input("Select an option: ").strip()

        if choice == "0":
            print("Goodbye.")
            break

        elif choice in ROUTES:
            module_path, name = ROUTES[choice]
            print(f"\n── {name} {'─' * (38 - len(name))}")
            try:
                mod = importlib.import_module(module_path)
                mod.run()
            except KeyboardInterrupt:
                print("\n\nInterrupted by user.")
            except Exception as e:
                print(f"\nError in {name}: {e}")
            print()
            print(BANNER)

        elif choice == "4":
            print(f"\n── Converter {'─' * 30}")
            try:
                _run_converter()
            except KeyboardInterrupt:
                print("\n\nInterrupted by user.")
            print()
            print(BANNER)

        else:
            print("Invalid option. Please enter 0 – 6.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = _build_parser()

    # If no arguments at all → interactive menu
    if len(sys.argv) == 1:
        _run_interactive()
        return

    args = parser.parse_args()

    # --batch overrides everything else
    if args.batch:
        _run_batch(args.batch)
        return

    if not args.site:
        parser.print_help()
        return

    try:
        _dispatch_cli(args)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")


if __name__ == "__main__":
    main()
