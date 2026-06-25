from playwright.sync_api import sync_playwright
import time
import os
import sys
import json
import re
from urllib.parse import urlparse

CACHE_FILE = "novel_cache.json"
SELECTORS_TEXT = [
    "div#chr-content",
    "div.chr-c", 
    "div.prose",
    "article p",
]

def sanitize_filename(name):
    """Removes illegal characters for Windows/Linux folder and file names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=4)

def get_text(page):
    for sel in SELECTORS_TEXT:
        try:
            if page.locator(sel).count() > 0:
                return page.locator(sel).inner_text(timeout=5000).strip()
        except Exception:
            continue
    try:
        paras = page.locator("p").all_inner_texts()
        return "\n".join(paras).strip()
    except Exception:
        return ""

def get_all_chapter_urls(page, base):
    page.goto(base, wait_until="networkidle", timeout=30000)
    time.sleep(2)
    
    try:
        page.locator("button:has-text('Chapters')").first.click(timeout=3000)
        time.sleep(2)
    except Exception:
        pass

    links = page.locator("a[href*='/chapter/']").all()
    all_hrefs = [l.get_attribute("href") for l in links if l.get_attribute("href")]
    
    urls = []
    seen = set()
    for href in reversed(all_hrefs):
        if href not in seen:
            seen.add(href)
            urls.append(href)
            
    urls.reverse()
    return urls

# --- MAIN EXECUTION ---
contents_url = input("Contents page URL: ").strip()
novel_name = input("Novel Name (Used for folder & file): ").strip()
base_dir = input("Base Save Directory (e.g., C:\\Books, or press Enter for current dir): ").strip()

safe_name = sanitize_filename(novel_name)

if not base_dir:
    base_dir = os.getcwd()

# Append 'output' folder to the base directory
output_dir = os.path.join(base_dir, "output")
novel_folder = os.path.join(output_dir, safe_name)
os.makedirs(novel_folder, exist_ok=True)

output_txt = os.path.join(novel_folder, f"{safe_name}.txt")
output_epub = os.path.join(novel_folder, f"{safe_name}.epub")

print(f"\n[Info] Files will be saved to: {novel_folder}")

cache = load_cache()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ))

    parsed = urlparse(contents_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"

    cached_urls = cache.get(contents_url, [])
    cached_len = len(cached_urls)

    if cached_len == 0:
        print("\n[Cache] Novel not found. Fetching chapter list from the web...")
        nav_page = ctx.new_page()
        all_urls = get_all_chapter_urls(nav_page, contents_url)
        nav_page.close()
        
        all_urls = list(reversed(all_urls))
        if not all_urls:
            print("Error: No chapters found on the website.")
            sys.exit(1)
            
        cache[contents_url] = all_urls
        save_cache(cache)
        cached_urls = all_urls
        cached_len = len(cached_urls)
    else:
        print(f"\n[Cache] Found {cached_len} chapters saved for this novel.")

    range_input = input(f"Enter range (e.g., 1-{cached_len}) or press Enter for all: ").strip()

    start_idx = 1
    end_idx = cached_len
    needs_refresh = False

    if range_input:
        try:
            start_str, end_str = range_input.split('-')
            start_idx = int(start_str.strip())
            end_idx = int(end_str.strip())
            
            if end_idx > cached_len:
                needs_refresh = True
        except ValueError:
            print(f"Error: Invalid format. Must be Start-End (e.g., 1-{cached_len}).")
            sys.exit(1)
    else:
        ans = input("Check the web for newly released chapters? (y/n): ").strip().lower()
        if ans == 'y':
            needs_refresh = True

    if needs_refresh:
        print("\nChecking for new chapters...")
        nav_page = ctx.new_page()
        all_urls = get_all_chapter_urls(nav_page, contents_url)
        nav_page.close()
        
        all_urls = list(reversed(all_urls))
        if len(all_urls) > cached_len:
            cache[contents_url] = all_urls
            save_cache(cache)
            cached_urls = all_urls
            print(f"Cache updated. Found {len(all_urls) - cached_len} new chapters. Total is now {len(all_urls)}.")
            cached_len = len(cached_urls)
        else:
            print("No new chapters found. Cache is up to date.")

    if not range_input or end_idx > cached_len:
        end_idx = cached_len

    if start_idx > end_idx or start_idx < 1:
        print("Error: Invalid range bounds.")
        sys.exit(1)

    target_urls = cached_urls[start_idx - 1 : end_idx]
    print(f"\nTargeting chapters {start_idx} to {end_idx}...")

    with open(output_txt, "w", encoding="utf-8") as f:
        for i, href in enumerate(target_urls, start=start_idx):
            url = href if href.startswith("http") else base_domain + href
            print(f"Chapter {i} → {url}")
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                time.sleep(2)
                text = get_text(page)
                if text:
                    f.write(f"\n\n=== CHAPTER {i} ===\n\n{text}")
                    print(f"  ✓ {len(text)} chars")
                else:
                    print(f"  ✗ no text")
            except Exception as e:
                print(f"  ERROR: {e}")
            finally:
                page.close()

    browser.close()

print(f"\nDone → {output_txt}")

# Check for the actual file instead of importing a potentially built-in module
if os.path.exists("parser.py"):
    convert = input("\nConvert to epub? (y/n): ").strip().lower()
    if convert == "y":
        os.system(f'python parser.py "{output_txt}" "{output_epub}"')
        print(f"Epub → {output_epub}")
else:
    print("\nNote: parser.py file not found in directory. EPUB conversion skipped.")