"""
core/cache.py — Unified cache management for the scraper suite.

Two variants to match the two different JSON structures already in use:

  NovelCache  — flat { url: [chapter_url, ...] }
                Used by: Novelbin, Novelarrow  →  novel_cache.json

  KafeCache   — nested { "novels": { url: { "drive_links": [...] } } }
                Used by: 9kafe                →  cache.json
"""
import json
import os
import time


class NovelCache:
    """
    Flat cache: maps a novel's base URL to its list of chapter URLs.
    Compatible with the existing novel_cache.json format.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)


class KafeCache:
    """
    Nested cache: maps a novel URL to its drive links and timestamp.
    Compatible with the existing cache.json format used by 9kafe.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {"novels": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "novels" not in data:
                    data["novels"] = {}
                return data
        except json.JSONDecodeError:
            print("Warning: cache.json is corrupted or empty. Starting fresh.")
            return {"novels": {}}
        except Exception as e:
            print(f"Error loading cache: {e}. Starting fresh.")
            return {"novels": {}}

    def save(self, data: dict) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except OSError as e:
            print(f"Error saving cache to disk: {e}")

    def update(self, url: str, epubs_data: list) -> None:
        """Writes a fresh list of drive links for the given URL and saves immediately."""
        data = self.load()
        data["novels"][url] = {
            "drive_links": epubs_data,
            "last_checked": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save(data)
