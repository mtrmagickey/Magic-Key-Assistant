"""Batch converts Discord .txt logs into JSONL-ready files with _lines suffix, in-place."""
import json
import re
from pathlib import Path


def process_single_file(input_file: Path) -> bool:
    """Process a single text file into _lines format. Returns True if processed."""
    if input_file.name.endswith("_lines.txt"):
        return False
    
    try:
        with input_file.open("r", encoding="utf-8") as infile:
            raw = infile.read()

        # Regex for Discord timestamp line
        pattern = r"\[\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} (AM|PM)] .+"
        splits = re.split(f"(?=({pattern}))", raw)
        messages = []

        for i in range(1, len(splits), 2):
            header = splits[i].strip()
            body = splits[i + 1].strip() if i + 1 < len(splits) else ""
            message = f"{header}\n{body}"
            messages.append(json.dumps({"text": message}))

        if not messages:
            return False

        output_file = input_file.with_name(f"{input_file.stem}_lines.txt")
        with output_file.open("w", encoding="utf-8") as out:
            out.write("\n".join(messages))
        return True
    except Exception as e:
        print(f"Error processing {input_file}: {e}")
        return False

def run_prep(docs_path: Path) -> int:
    count = 0
    if not docs_path.exists():
        return 0
        
    for input_file in docs_path.glob("*.txt"):
        if process_single_file(input_file):
            count += 1
            print(f"✔ Processed: {input_file.name}")
    return count

if __name__ == "__main__":
    # Default behavior
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    run_prep(docs_dir)
