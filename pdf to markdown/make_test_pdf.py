"""Generate a synthetic multi-page PDF to exercise the cleanup pipeline."""
import pymupdf  # provided by pymupdf4llm's dependency

doc = pymupdf.open()
RUNNING_HEADER = "THINKING, FAST AND SLOW"

pages_text = [
    # page 1
    f"{RUNNING_HEADER}\n\nPart 1 Two Systems\n\n"
    "This is the opening paragraph of the book. It introduces the two\n"
    "systems of thinking that the author will discuss throughout.\n\n1",
    # page 2
    f"{RUNNING_HEADER}\n\n1. The Characters of the Story\n\n"
    "System 1 operates automatically and quickly, with little effort and no\n"
    "sense of voluntary control. This sentence is deliberately cut off here and\n"
    "continues onto the next page because the paragraph was split by a page\n2",
    # page 3
    f"{RUNNING_HEADER}\n\n"
    "break in the original layout. System 2 allocates attention to effortful\n"
    "mental activities.\n\nChapter 2 Attention and Effort\n\n"
    "The pupil of the eye is a useful index of mental effort.\n\n3",
]

for body in pages_text:
    page = doc.new_page()
    page.insert_text((72, 72), body, fontsize=11)

out = "test_sample.pdf"
doc.save(out)
print(f"wrote {out} with {len(pages_text)} pages")
