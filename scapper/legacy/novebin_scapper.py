import os
import json
import time
import re
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# --- 1. User Inputs ---
chapter_url = input("Chapter URL (to fetch list from or start at): ").strip()
novel_name = input("Enter Novel Name (used for folder and file names): ").strip()

# --- 2. Directory Setup ---
output_base_dir = "output"
novel_dir = os.path.join(output_base_dir, novel_name)
os.makedirs(novel_dir, exist_ok=True)

output_file = os.path.join(novel_dir, f"{novel_name}.txt")
cache_file = "novel_cache.json"

# --- 3. Base URL Extraction ---
# Extracts domain + next two paths (e.g., https://novelbin.com/b/lord-of-the-truth)
match = re.match(r"^(https?://[^/]+/[^/]+/[^/]+)", chapter_url)
if not match:
    print("Could not parse Base URL from the provided Chapter URL. Exiting.")
    exit()
base_url = match.group(1)
print(f"Detected Base URL for cache: {base_url}")

SELECTORS_TEXT = [
    "div#chr-content",
    "div.chr-c",
    "div.reading-content",
    "div#content",
    "article p",
]

def get_text(page):
    for sel in SELECTORS_TEXT:
        try:
            if page.locator(sel).count() > 0:
                return page.locator(sel).inner_text(timeout=5000).strip()
        except:
            continue
    try:
        return "\n".join(page.locator("p").all_inner_texts()).strip()
    except:
        return ""

def fetch_urls_from_site(page, url):
    print("Visiting site to fetch chapter list...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3) 
    
    try:
        btn = page.locator("button.chr-jump").first
        if btn.count() > 0:
            btn.click(timeout=5000)
            page.wait_for_selector("select.chr-jump option", state="attached", timeout=10000)
    except Exception as e:
        print(f"Button click failed/ignored: {e}")

    options = page.locator("select.chr-jump").first.locator("option").all()
    raw_urls = [o.get_attribute("value") for o in options if o.get_attribute("value")]
    return list(dict.fromkeys(raw_urls))

def load_cache():
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}

def save_cache(data):
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# --- 4. Cache & Navigation Logic ---
cache_data = load_cache()
all_urls = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ))

    # Check cache presence
    if base_url in cache_data and cache_data[base_url]:
        cached_urls = cache_data[base_url]
        print(f"\nFound {len(cached_urls)} chapters in cache for this novel.")
        print("[1] Use existing cached URLs")
        print("[2] Visit site to append new URLs")
        choice = input("Select option (1/2): ").strip()
        
        if choice == "2":
            nav = ctx.new_page()
            scraped_urls = fetch_urls_from_site(nav, chapter_url)
            nav.close()
            
            existing_set = set(cached_urls)
            added_count = 0
            for u in scraped_urls:
                if u not in existing_set:
                    cached_urls.append(u)
                    existing_set.add(u)
                    added_count += 1
                    
            print(f"Appended {added_count} new URLs. Total is now {len(cached_urls)}.")
            cache_data[base_url] = cached_urls
            save_cache(cache_data)
            all_urls = cached_urls
        else:
            print("Using cached URLs.")
            all_urls = cached_urls
    else:
        nav = ctx.new_page()
        all_urls = fetch_urls_from_site(nav, chapter_url)
        nav.close()
        
        if not all_urls:
            print("0 chapters found. Exiting.")
            browser.close()
            exit()
            
        print(f"Found {len(all_urls)} unique chapters. Saving to cache.")
        cache_data[base_url] = all_urls
        save_cache(cache_data)

    # --- 5. Chapter Range Logic ---
    print(f"\nTotal available chapters: {len(all_urls)}")
    range_input = input("Enter range (e.g. '10-50', '10-', '-50') or press Enter for ALL: ").strip()
    
    start_idx, end_idx = 0, len(all_urls)
    
    if range_input:
        parts = range_input.split('-')
        if len(parts) == 2:
            if parts[0].isdigit():
                start_idx = max(0, int(parts[0]) - 1)
            if parts[1].isdigit():
                end_idx = min(len(all_urls), int(parts[1]))
        else:
            print("Invalid format. Using ALL chapters.")
            
    urls_to_scrape = all_urls[start_idx:end_idx]
    print(f"\nScraping {len(urls_to_scrape)} chapters (from chapter {start_idx + 1} to {end_idx})...")

    # --- 6. Scraping Loop ---
    with open(output_file, "w", encoding="utf-8") as f:
        for i, url in enumerate(urls_to_scrape, start=start_idx + 1):
            target_url = urljoin(base_url + "/", url)
            print(f"Ch {i} → {target_url}")
            
            page = ctx.new_page()
            try:
                page.goto(target_url, wait_until="networkidle", timeout=30000)
                time.sleep(2)
                text = get_text(page)
                if text:
                    f.write(f"\n\n=== CHAPTER {i} ===\n\n{text}")
                    print(f"  ✓ {len(text)} chars")
                else:
                    print(f"  ✗ no text")
            except Exception as e:
                print(f"  ERR: {e}")
            finally:
                page.close()

    browser.close()

print(f"\nDone → {output_file}")

conv = input("Convert to epub? (y/n): ").strip().lower()
if conv == "y":
    epub_file = os.path.join(novel_dir, f"{novel_name}.epub")
    os.system(f"py parser.py \"{output_file}\" \"{epub_file}\"")