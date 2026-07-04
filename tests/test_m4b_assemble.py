import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from m4b import assemble_m4b


def _sine_wav(path: Path, seconds: float, freq: float):
    t = np.linspace(0, seconds, int(24000 * seconds), endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32),
             24000, subtype="PCM_16")


def test_assemble_produces_chaptered_m4b(tmp_path):
    w1, w2 = tmp_path / "c1.wav", tmp_path / "c2.wav"
    _sine_wav(w1, 1.0, 440); _sine_wav(w2, 1.5, 660)
    out = tmp_path / "book.m4b"

    result = assemble_m4b([("Ch One", w1), ("Ch Two", w2)], out,
                          book_title="Test Book", author="Tester")

    assert result == out and out.exists()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    data = json.loads(probe.stdout)
    titles = [c["tags"]["title"] for c in data["chapters"]]
    assert titles == ["Ch One", "Ch Two"]
    assert abs(float(data["format"]["duration"]) - 2.5) < 0.2
    assert data["format"]["tags"].get("title") == "Test Book"


def test_assemble_failure_raises_and_cleans_up(tmp_path):
    w1 = tmp_path / "c1.wav"
    _sine_wav(w1, 1.0, 440)
    out = tmp_path / "book.m4b"

    with pytest.raises(RuntimeError, match="M4B encode failed"):
        assemble_m4b([("Ch One", w1)], out,
                     book_title="Test Book", author="Tester",
                     bitrate="not-a-bitrate")

    assert not out.exists()
    assert not (tmp_path / "combined.wav").exists()
    assert not (tmp_path / "concat_list.txt").exists()
    assert not (tmp_path / "metadata.txt").exists()
