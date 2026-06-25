import os
import sys

try:
    import scapper.legacy.parser as parser
except ImportError:
    print("Error: parser.py module not found in the current directory.")
    sys.exit(1)

target_dir = input("Enter the directory path (or press Enter for current directory): ").strip()

if not target_dir:
    target_dir = os.getcwd()

if not os.path.isdir(target_dir):
    print(f"Error: The directory '{target_dir}' does not exist.")
    sys.exit(1)

# Retrieve all .txt files in the directory
txt_files = [f for f in os.listdir(target_dir) if f.lower().endswith(".txt")]

if not txt_files:
    print(f"Error: No .txt files found in '{target_dir}'.")
    sys.exit(1)

print(f"\nFound {len(txt_files)} text file(s) in {target_dir}:")
for i, file in enumerate(txt_files, 1):
    print(f"[{i}] {file}")

choice = input("\nEnter the number of the file to convert (or type 'all'): ").strip().lower()

files_to_convert = []
if choice == 'all':
    files_to_convert = txt_files
else:
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(txt_files):
            files_to_convert.append(txt_files[idx])
        else:
            print("Error: Invalid number selected.")
            sys.exit(1)
    except ValueError:
        print("Error: Invalid input. Must be a number or 'all'.")
        sys.exit(1)

for txt_file in files_to_convert:
    input_path = os.path.join(target_dir, txt_file)
    output_name = txt_file.rsplit('.', 1)[0] + ".epub"
    output_path = os.path.join(target_dir, output_name)
    
    print(f"\nConverting '{txt_file}'...")
    try:
        parser.txt_to_epub(input_path, output_path)
    except Exception as e:
        print(f"  ERROR failed to convert {txt_file}: {e}")

print("\nConversion process finished.")