#!/usr/bin/env python3
"""Convert ALL CAPS words to Title Case in the TTS file."""

import re
from pathlib import Path

file_path = Path("PROJECT_OVERVIEW_TTS.txt")
content = file_path.read_text()

# Pattern to match words that are all caps (2+ letters)
# But preserve single letters like I or A
def convert_caps_word(match):
    word = match.group(0)
    # Keep single letters as-is
    if len(word) == 1:
        return word
    # Convert to title case (first letter cap, rest lowercase)
    return word.capitalize()

# Replace all sequences of 2+ capital letters with title case
converted = re.sub(r'\b[A-Z]{2,}\b', convert_caps_word, content)

# Write back
file_path.write_text(converted)
print(f"✓ Converted ALL CAPS to Title Case in {file_path}")
print("Converted examples: EXTRACTOR→Extractor, VALIDATOR→Validator, ROUTER→Router, etc.")
