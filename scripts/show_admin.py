import sys
from pathlib import Path

# Direct script execution starts Python's import path in scripts/, including in Docker.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_settings
print(load_settings().admin_token)
