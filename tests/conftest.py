"""Test bootstrap: point the app at throwaway state before any project module
is imported (each of these is read at import time).

The audio directory matters most. Starting the app applies the retention
policy, which deletes every chapter file the database does not account for —
and the test database accounts for nothing. Ten test modules start the app via
`with TestClient(app)`, so running the suite against the real ./temp_audio
destroyed the whole cache: hours of rendered audio, gone, with the loss
quietly blamed on server restarts.
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="noveltts_test_")
os.environ["NOVEL_TTS_DB"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["NOVEL_TTS_EPUB_DIR"] = f"{_tmpdir}/EPUBs"
os.environ["NOVEL_TTS_TEMP_AUDIO"] = f"{_tmpdir}/temp_audio"
