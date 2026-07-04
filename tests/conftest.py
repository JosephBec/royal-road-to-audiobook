"""Test bootstrap: point the app at a throwaway SQLite DB before any
project module is imported (database.py reads NOVEL_TTS_DB at import time)."""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="noveltts_test_")
os.environ["NOVEL_TTS_DB"] = f"sqlite:///{_tmpdir}/test.db"
