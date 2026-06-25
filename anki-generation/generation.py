import re
import sys
import os
import requests



# ── CONFIG ──────────────────────────────────────────
ANKI_URL = "http://localhost:8765"
# Force the script to look in its own exact folder
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FILE_NAME = os.path.join(SCRIPT_DIR, "cards.txt")

# ── FILE READER ──────────────────────────────────────
def read_cards_file(filename):
    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        print(f"Error: '{filename}' is missing or empty.")
        sys.exit(1)
    with open(filename, 'r', encoding='utf-8') as f:
        return f.read()

# ── PARSER ───────────────────────────────────────────
def parse_cards(text):
    pattern = re.compile(
        r'Deck:\s*(.+?)(?:\s+Card Type:\s*(.+?))?\s*\n'
        r'Front:\s*(.+?)\s*\n'
        r'Back:\s*(.+?)(?=\n\s*\nDeck:|\Z)',
        re.DOTALL
    )
    cards = []
    for m in pattern.finditer(text):
        cards.append({
            "deck": m.group(1).strip(),
            "type": m.group(2).strip() if m.group(2) else "Basic",
            "front": m.group(3).strip(),
            "back": m.group(4).strip()
        })
    return cards

# ── ANKICONNECT HELPERS ──────────────────────────────
def anki(action, **params):
    payload = {"action": action, "version": 6, "params": params}
    try:
        r = requests.post(ANKI_URL, json=payload)
        result = r.json()
        if result.get("error"):
            raise Exception(f"AnkiConnect error: {result['error']}")
        return result["result"]
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to Anki. Is Anki open with AnkiConnect installed?")
        sys.exit(1)

def ensure_deck(deck_name):
    anki("createDeck", deck=deck_name)

def add_card(deck, model_type, front, back):
    # Map Anki fields correctly depending on the model
    if "cloze" in model_type.lower():
        fields = {"Text": front, "Extra": back}
    else:
        fields = {"Front": front, "Back": back}

    return anki("addNote", note={
        "deckName": deck,
        "modelName": model_type,
        "fields": fields,
        "options": {"allowDuplicate": False},
        "tags": []
    })

# ── MAIN ─────────────────────────────────────────────
def main():
    raw_text = read_cards_file(FILE_NAME)
    cards = parse_cards(raw_text)
    
    if not cards:
        print("Error: No valid cards parsed from file. Check your formatting.")
        sys.exit(1)

    print(f"Parsed {len(cards)} cards.\n")

    # Fetch and display existing decks
    decks = anki("deckNames")
    print("Available Decks in Anki:")
    for i, d in enumerate(decks, 1):
        print(f"  [{i}] {d}")
    print("  [0] Create a NEW deck")

    # Handle user selection
    try:
        choice = int(input("\nSelect a deck number (0 to create new): ").strip())
        if choice == 0:
            target_deck = input("Enter new deck name: ").strip()
        else:
            target_deck = decks[choice - 1]
    except (ValueError, IndexError):
        print("Invalid selection. Exiting.")
        sys.exit(1)

    print(f"\nTarget Deck: {target_deck}")
    ensure_deck(target_deck)

    # Insert cards
    for c in cards:
        try:
            note_id = add_card(target_deck, c["type"], c["front"], c["back"])
            print(f"✓ Added [{note_id}] ({c['type']}) → {c['front'][:40]}...")
        except Exception as e:
            print(f"✗ Failed → {e}")

    # Clear the file after successful processing
    with open(FILE_NAME, 'w', encoding='utf-8') as f:
        f.truncate(0)
    print(f"Cleared contents of '{FILE_NAME}'.")        

if __name__ == "__main__":
    main()