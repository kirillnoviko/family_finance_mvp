import os
from pathlib import Path
import pytest

TEST_DB = '/tmp/family-finance-pytest.db'
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-100')
os.environ.setdefault('API_SECRET', 'test')
os.environ['DATABASE_PATH'] = TEST_DB
os.environ.setdefault('PERIOD_START_DAY', '15')

@pytest.fixture(autouse=True)
def clean_test_database():
    for suffix in ('', '-wal', '-shm'):
        try:
            Path(TEST_DB + suffix).unlink()
        except FileNotFoundError:
            pass
    yield
    for suffix in ('', '-wal', '-shm'):
        try:
            Path(TEST_DB + suffix).unlink()
        except FileNotFoundError:
            pass
