"""Paths shared by the bot and web panel."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ['APP_DATA_DIR']).resolve() if os.environ.get('APP_DATA_DIR') else ROOT
CONFIG = DATA_DIR / 'config.json'
SECRETS = DATA_DIR / '.secrets.json'
RUNTIME = DATA_DIR if os.environ.get('APP_DATA_DIR') else ROOT / 'data'
STATUS = RUNTIME / 'status.json'
DELIVERIES = RUNTIME / 'deliveries.sqlite3'
