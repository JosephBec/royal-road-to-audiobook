"""Running the Chatterbox transformer in fp16.

Worth ~0.9 GB of VRAM on an 8 GB card. The risk is not that it is slow, it is
that fp16 overflows silently: values above 65504 become inf, then NaN, then
silence or noise that nobody notices until they are listening to a chapter.

So the conversion has to prove itself on real audio and undo itself cleanly
when it cannot. These tests are about that recovery, not about speed. No GPU
and no model — the pieces under test are the dtype plumbing and the fallback.
"""
import sys
import types

import numpy as np
import pytest

from engines.chatterbox_engine import ChatterboxEngine


class FakeTensor:
    """Minimal stand-in: only dtype and .half() matter here."""

    def __init__(self, dtype="float32"):
        self.dtype = dtype

    def half(self):
        return FakeTensor("float16")


class FakeT3Cond:
    __dataclass_fields__ = {"speaker_emb": None, "emotion_adv": None,
                            "cond_prompt_speech_tokens": None}

    def __init__(self):
        self.speaker_emb = FakeTensor()
        self.emotion_adv = FakeTensor()
        self.cond_prompt_speech_tokens = FakeTensor("int64")   # must not change


class FakeConds:
    def __init__(self):
        self.t3 = FakeT3Cond()


@pytest.fixture()
def fake_torch(monkeypatch):
    """torch.is_tensor / memory_allocated, without importing torch."""
    torch = types.ModuleType("torch")
    torch.is_tensor = lambda o: isinstance(o, FakeTensor)
    torch.float32 = "float32"
    torch.cuda = types.SimpleNamespace(memory_allocated=lambda: 2_420_000_000,
                                       empty_cache=lambda: None)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


# ----- casting the conditioning tensors -----

def test_float_conditionals_are_cast(fake_torch):
    conds = FakeConds()
    ChatterboxEngine()._cast_conds(conds)
    assert conds.t3.speaker_emb.dtype == "float16"
    assert conds.t3.emotion_adv.dtype == "float16"


def test_integer_conditionals_are_left_alone(fake_torch):
    """Token ids are indices. Casting them to fp16 would corrupt them."""
    conds = FakeConds()
    ChatterboxEngine()._cast_conds(conds)
    assert conds.t3.cond_prompt_speech_tokens.dtype == "int64"


def test_casting_survives_conditionals_without_a_t3(fake_torch):
    ChatterboxEngine()._cast_conds(types.SimpleNamespace(t3=None))   # no raise


# ----- proving the output is real audio -----

def _engine_producing(audio):
    engine = ChatterboxEngine()

    class Wav:
        def squeeze(self, _):
            return self

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return audio

    engine._model = types.SimpleNamespace(generate=lambda text: Wav())
    return engine


def test_normal_audio_passes():
    audio = (np.random.default_rng(0).standard_normal(24000) * 0.1).astype(np.float32)
    assert _engine_producing(audio)._half_output_is_sane() is True


def test_nan_output_is_rejected():
    """The fp16 overflow signature: inf, then NaN."""
    audio = np.full(24000, np.nan, dtype=np.float32)
    assert _engine_producing(audio)._half_output_is_sane() is False


def test_inf_output_is_rejected():
    audio = np.zeros(24000, dtype=np.float32)
    audio[100] = np.inf
    assert _engine_producing(audio)._half_output_is_sane() is False


def test_silence_is_rejected():
    """Finite but silent is still a broken render, and the likelier symptom."""
    audio = np.zeros(24000, dtype=np.float32)
    assert _engine_producing(audio)._half_output_is_sane() is False


def test_empty_output_is_rejected():
    assert _engine_producing(np.zeros(0, dtype=np.float32))._half_output_is_sane() is False


# ----- the fallback, which is the whole safety argument -----

def test_bad_audio_triggers_a_clean_reload(fake_torch, monkeypatch):
    """A half-applied conversion is worse than none.

    The module would be fp16 with fp32 inputs and every render would fail, so
    recovery throws the model away and reloads rather than trying to undo it.
    """
    engine = ChatterboxEngine()
    engine._model = types.SimpleNamespace(t3=FakeTensor(), conds=FakeConds())
    engine._builtin_conds = FakeConds()
    monkeypatch.setattr(engine, "_half_output_is_sane", lambda: False)
    reloaded = []
    monkeypatch.setattr(engine, "_reload_fp32", lambda: reloaded.append(True))

    engine._try_half_precision()

    assert engine._half is False, "must not claim fp16 after a failed check"
    assert reloaded == [True]
    assert engine.precision == "fp32"


def test_an_exception_during_conversion_also_reloads(fake_torch, monkeypatch):
    engine = ChatterboxEngine()

    class Exploding:
        def half(self):
            raise RuntimeError("no half for you")

    engine._model = types.SimpleNamespace(t3=Exploding(), conds=FakeConds())
    engine._builtin_conds = FakeConds()
    reloaded = []
    monkeypatch.setattr(engine, "_reload_fp32", lambda: reloaded.append(True))

    engine._try_half_precision()   # must not propagate

    assert engine._half is False
    assert reloaded == [True]


def test_success_marks_the_engine_fp16(fake_torch, monkeypatch):
    engine = ChatterboxEngine()
    engine._model = types.SimpleNamespace(t3=FakeTensor(), conds=FakeConds())
    engine._builtin_conds = FakeConds()
    monkeypatch.setattr(engine, "_half_output_is_sane", lambda: True)
    monkeypatch.setattr(engine, "_reload_fp32",
                        lambda: pytest.fail("must not reload on success"))

    engine._try_half_precision()

    assert engine._half is True
    assert engine.precision == "fp16"
    assert engine._model.t3.dtype == "float16"


# ----- precision is part of a render's identity -----

def test_a_fresh_engine_reports_fp32():
    assert ChatterboxEngine().precision == "fp32"


def test_unload_forgets_fp16(fake_torch):
    """Otherwise a reload could inherit a claim that no longer holds."""
    engine = ChatterboxEngine()
    engine._half = True
    engine._model = types.SimpleNamespace()
    engine.unload()
    assert engine._half is False
    assert engine.precision == "fp32"
