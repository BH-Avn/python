import re
import sys
from ebooklib import epub

def txt_to_epub(input_file, output_file):
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: '{input_file}' not found.")
        sys.exit(1)

    # regex to split strictly by '=== CHAPTER <number> ==='
    chunks = re.split(r'(=== CHAPTER \d+ ===)', content)
    
    if len(chunks) < 3:
        print("Error: No '=== CHAPTER N ===' markers found.")
        sys.exit(1)

    book = epub.EpubBook()
    book.set_identifier("novel123")
    book.set_title("Generated Novel")
    book.set_language("en")

    chapters = []
    
    for i in range(1, len(chunks), 2):
        chapter_title = chunks[i].strip('=').strip()
        chapter_raw_text = chunks[i+1].strip()
        
        if not chapter_raw_text:
            continue

        html_paragraphs = [f"<p>{line.strip()}</p>" for line in chapter_raw_text.split('\n') if line.strip()]
        html_content = f"<h1>{chapter_title}</h1>\n" + "\n".join(html_paragraphs)
        
        file_name = f"chapter_{len(chapters)+1}.xhtml"
        chapter = epub.EpubHtml(title=chapter_title, file_name=file_name, lang="en")
        chapter.content = html_content
        
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav'] + chapters

    epub.write_epub(output_file, book, {})
    print(f"Success: '{output_file}' generated with {len(chapters)} chapters.")

if __name__ == "__main__":
    # Ensure correct number of arguments are passed via terminal/os.system
    if len(sys.argv) != 3:
        print("Usage: py parser.py <input_file.txt> <output_file.epub>")
        sys.exit(1)
        
    input_arg = sys.argv[1]
    output_arg = sys.argv[2]
    
    txt_to_epub(input_arg, output_arg)