from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

chapter_url = input("Chapter URL: ").strip()

# Wrap sync_playwright() with the new Stealth class
with Stealth().use_sync(sync_playwright()) as p:
    # 1. Load the pre-configured mobile profile
    mobile_device = p.devices['Pixel 5']
    
    browser = p.chromium.launch(headless=False)
    
    # 2. Apply the mobile profile. Stealth is now injected automatically into all contexts.
    ctx = browser.new_context(**mobile_device)
    page = ctx.new_page()
    
    print("\nNavigating as a Mobile Device...")
    page.goto(chapter_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4) 
    
    try:
        print("Triggering chapter dropdown...")
        btn = page.locator("button.chr-jump").first
        if btn.count() > 0:
            btn.click(timeout=5000)
            page.wait_for_selector("select.chr-jump option", state="attached", timeout=10000)
    except Exception as e:
        print(f"Dropdown interaction failed: {e}")

    # Extract and deduplicate
    options = page.locator("select.chr-jump").first.locator("option").all()
    raw_urls = [o.get_attribute("value") for o in options if o.get_attribute("value")]
    unique_urls = list(dict.fromkeys(raw_urls))
    
    print(f"\nTotal Unique Chapters Found: {len(unique_urls)}")
    
    if unique_urls:
        print(f"First URL: {unique_urls[0]}")
        print(f"Last URL:  {unique_urls[-1]}")
        
    browser.close()