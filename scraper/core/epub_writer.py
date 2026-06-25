"""
core/epub_writer.py — Converts a structured .txt file to a .epub.

Ported from parser.py. Now importable as a module instead of being called
via os.system(), which was fragile (depended on correct CWD and Python alias).

Expected input format
---------------------
    === CHAPTER 1 ===
    Chapter text here...

    === CHAPTER 2 ===
    More text...
"""
import re
import sys

try:
    from ebooklib import epub
except ImportError:
    print("Error: ebooklib not installed. Run: pip install ebooklib")
    sys.exit(1)


def txt_to_epub(input_file: str, output_file: str) -> None:
    """
    Reads a chapter-delimited .txt file and writes a properly structured .epub.

    Args:
        input_file:  Absolute path to the source .txt file.
        output_file: Absolute path where the .epub will be written.
    """
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: '{input_file}' not found.")
        return

    # Split on chapter markers, keeping the markers as separate tokens
    chunks = re.split(r"(=== CHAPTER \d+ ===)", content)

    if len(chunks) < 3:
        print("Error: No '=== CHAPTER N ===' markers found in the file.")
        return

    book = epub.EpubBook()
    book.set_identifier("novel123")
    book.set_title("Generated Novel")
    book.set_language("en")

    chapters = []

    for i in range(1, len(chunks), 2):
        chapter_title = chunks[i].strip("=").strip()
        chapter_raw_text = chunks[i + 1].strip()

        if not chapter_raw_text:
            continue

        html_paragraphs = [
            f"<p>{line.strip()}</p>"
            for line in chapter_raw_text.split("\n")
            if line.strip()
        ]
        html_content = f"<h1>{chapter_title}</h1>\n" + "\n".join(html_paragraphs)

        file_name = f"chapter_{len(chapters) + 1}.xhtml"
        chap = epub.EpubHtml(title=chapter_title, file_name=file_name, lang="en")
        chap.content = html_content

        book.add_item(chap)
        chapters.append(chap)

    if not chapters:
        print("Error: No chapters with content were found.")
        return

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    epub.write_epub(output_file, book, {})
    print(f"✓ EPUB generated: '{output_file}' ({len(chapters)} chapters)")
