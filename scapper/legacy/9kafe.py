import os
import re
import sys
import json
import time

try:
    import requests
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Dependencies missing. Install via terminal:\n  pip install requests playwright\n  playwright install")
    sys.exit(1)

# --- Configuration & Paths ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "cache.json")
DOWNLOAD_BASE_DIR = os.path.join(BASE_DIR, "downloaded_epubs")

# ==========================================
# 1. CACHE MANAGEMENT
# ==========================================

def load_cache():
    """Loads the JSON cache. Recovers gracefully if missing or corrupted."""
    if not os.path.exists(CACHE_FILE):
        return {"novels": {}}
    
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Ensure the base structure exists
            if "novels" not in data:
                data["novels"] = {}
            return data
    except json.JSONDecodeError:
        print("Warning: cache.json is corrupted or empty. Creating a new empty cache.")
        return {"novels": {}}
    except Exception as e:
        print(f"Error loading cache: {e}. Defaulting to empty cache.")
        return {"novels": {}}

def save_cache(cache_data):
    """Saves data back to the JSON cache file safely."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=4)
    except OSError as e:
        print(f"Error saving cache to disk: {e}")

def update_cache(url, epubs_data):
    """Updates the cache with new scraped links and saves it."""
    cache_data = load_cache()
    cache_data["novels"][url] = {
        "drive_links": epubs_data,
        "last_checked": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_cache(cache_data)

# ==========================================
# 2. LINK CONVERSION & SCRAPING
# ==========================================

def convert_drive_to_direct_link(gdrive_url):
    """Extracts the ID from a Google Drive link and returns a direct download URL."""
    # Matches ?id=ID or &id=ID
    file_id_match = re.search(r"id=([a-zA-Z0-9_-]+)", gdrive_url)
    
    # Fallback for /file/d/ID/view format
    if not file_id_match:
        file_id_match = re.search(r"/d/([a-zA-Z0-9_-]+)", gdrive_url)
        
    if not file_id_match:
        return None
        
    file_id = file_id_match.group(1)
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

def scrape_novel_page(novel_url, slug):
    """Uses Playwright to bypass JS/Cloudflare and extract Drive links."""
    epubs = []
    base = novel_url.rstrip("/")
    
    print("\nStarting Playwright scraper to fetch links...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # Auto-close ad popups
            page.on("popup", lambda popup: popup.close())

            # Speed up loading by blocking media
            def intercept_route(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", intercept_route)

            i = 0
            consecutive_failures = 0
            MAX_FAILURES = 3

            while True:
                if consecutive_failures >= MAX_FAILURES:
                    print(f"Hit {MAX_FAILURES} consecutive empty/failed pages. Assuming end of list.")
                    break

                page_url = f"{base}/download/{i}/"
                print(f"[{i}] Checking: {page_url}")

                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                    
                    # Wait for any Google Drive link to be attached to the DOM
                    page.wait_for_selector("a[href*='drive.google.com']", state="attached", timeout=10000)
                    a_locator = page.locator("a[href*='drive.google.com']").first
                    gdrive_url = a_locator.get_attribute("href")

                    if not gdrive_url:
                        raise Exception("No href attribute found.")

                except Exception as e:
                    print(f"  Failed to find GDrive link. ({str(e).splitlines()[0]})")
                    consecutive_failures += 1
                    i += 1
                    continue

                # Reset failures upon a successful find
                consecutive_failures = 0
                direct_url = convert_drive_to_direct_link(gdrive_url)
                
                if direct_url:
                    filename = f"{slug}_part{i}.epub"
                    epubs.append({
                        "filename": filename,
                        "direct_download_url": direct_url
                    })
                    print(f"  Found valid link for: {filename}")
                else:
                    print(f"  Could not extract valid Drive ID from: {gdrive_url}")

                i += 1
                time.sleep(1) # Polite delay

            browser.close()
    except Exception as e:
        print(f"Critical error during Playwright scraping: {e}")

    return epubs

# ==========================================
# 3. INTERACTIVE SELECTION
# ==========================================

def ask_cache_or_recheck():
    """Prompts the user when a cached URL is found."""
    print("\nThis URL already exists in cache.")
    while True:
        choice = input("1. Use cached data\n2. Recheck website for updated links\nChoose: ").strip()
        if choice == "1":
            return "cache"
        elif choice == "2":
            return "recheck"
        print("Invalid choice. Please enter 1 or 2.")

def show_epub_selection(epubs):
    """Displays available files and safely processes user selection."""
    print("\nAvailable EPUB files:")
    for idx, epub in enumerate(epubs):
        print(f"{idx + 1}. {epub['filename']}")

    while True:
        choice = input("\nEnter numbers separated by commas to download specific files.\nPress Enter to download ALL files.\nSelection: ").strip()
        
        # User pressed Enter
        if not choice:
            return epubs

        try:
            # Parse commas and strip whitespace
            indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
            
            # Check bounds
            invalid = [x for x in indices if x < 1 or x > len(epubs)]
            if invalid:
                print(f"Error: Numbers out of range {invalid}. Please try again.")
                continue

            # Deduplicate and sort (e.g. user entered "1, 1, 3, 2")
            indices = sorted(list(set(indices)))
            
            # Map back to 0-indexed array
            selected_epubs = [epubs[i - 1] for i in indices]
            return selected_epubs
            
        except ValueError:
            print("Error: Invalid input. Please enter valid numbers separated by commas.")

# ==========================================
# 4. DOWNLOAD & DIRECTORY MANAGEMENT
# ==========================================

def create_download_directory(slug):
    """Creates base and novel-specific directories securely."""
    folder_path = os.path.join(DOWNLOAD_BASE_DIR, slug)
    try:
        os.makedirs(folder_path, exist_ok=True)
    except OSError as e:
        print(f"Error creating directory {folder_path}: {e}")
        sys.exit(1)
    return folder_path

def handle_existing_file(filepath):
    """Prompts user action if the EPUB already exists."""
    filename = os.path.basename(filepath)
    print(f"\n{filename} already exists.")
    while True:
        choice = input("1. Skip\n2. Redownload and overwrite\nChoose: ").strip()
        if choice == "1":
            return False
        elif choice == "2":
            return True
        print("Invalid choice. Please enter 1 or 2.")

def download_epub(url, filepath):
    """Downloads the file with chunking and handles network/disk errors."""
    filename = os.path.basename(filepath)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        dl = requests.get(url, headers=headers, timeout=60, stream=True)
        dl.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in dl.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"  Successfully saved: {filename} ({size_mb:.1f} MB)")
        
    except requests.exceptions.RequestException as e:
        print(f"  Network error while downloading {filename}: {e}")
    except OSError as e:
        print(f"  Disk/Permissions error saving {filename}: {e}")

# ==========================================
# 5. MAIN ORCHESTRATION
# ==========================================

def main():
    try:
        url_input = input("Enter the novel URL (e.g., https://9kafe.com/.../): ").strip()
        if not url_input.startswith("http"):
            print("Error: Invalid URL. Must start with http or https.")
            return

        base_url = url_input.rstrip("/")
        slug = base_url.split("/")[-1]

        cache_data = load_cache()
        epubs = []

        # --- URL Lookup & Routing ---
        if base_url in cache_data.get("novels", {}):
            action = ask_cache_or_recheck()
            if action == "cache":
                epubs = cache_data["novels"][base_url].get("drive_links", [])
                print("Loaded link data from cache.")
            elif action == "recheck":
                epubs = scrape_novel_page(base_url, slug)
                update_cache(base_url, epubs)
        else:
            epubs = scrape_novel_page(base_url, slug)
            update_cache(base_url, epubs)

        # --- Post-Fetch Validation ---
        if not epubs:
            print("No valid EPUB links were found or loaded. Exiting.")
            return

        # --- Selection ---
        selected_epubs = show_epub_selection(epubs)
        if not selected_epubs:
            print("No files selected to download.")
            return

        # --- Download Execution ---
        target_dir = create_download_directory(slug)
        print(f"\nFiles will be saved to: {target_dir}")

        for item in selected_epubs:
            filename = item["filename"]
            direct_url = item["direct_download_url"]
            filepath = os.path.join(target_dir, filename)

            if os.path.exists(filepath):
                should_download = handle_existing_file(filepath)
                if not should_download:
                    print(f"  Skipped: {filename}")
                    continue

            print(f"\nDownloading → {filename} ...")
            download_epub(direct_url, filepath)
            time.sleep(1) # Polite delay between requests

        print("\nAll operations completed.")

    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting.")
        sys.exit(0)

if __name__ == "__main__":
    main()