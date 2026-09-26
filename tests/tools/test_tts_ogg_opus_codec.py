"""Regression coverage for forcing Ogg/Opus (not ffmpeg's default Vorbis)
on .ogg output for the local TTS providers.

Telegram's iOS voice-bubble player requires a true Opus payload inside the
.ogg container. ffmpeg's default codec for a bare ".ogg" output filename is
Vorbis, which plays fine on desktop/tablet Telegram clients but silently
fails to render as a voice bubble on iOS. _generate_neutts,
_generate_piper_tts, and _generate_kittentts each convert their native WAV
output to the caller's requested format via ffmpeg; this module pins that
every one of them forces "-acodec libopus" (not ffmpeg's Vorbis default)
whenever the requested output path ends in ".ogg", and that a non-.ogg
target (e.g. .mp3) is left on ffmpeg's default codec selection.
"""

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools import tts_tool


def _fake_ffmpeg_run(calls):
    """Return a subprocess.run stand-in that records ffmpeg conv commands
    and materializes the requested output file, without invoking a real
    ffmpeg binary."""

    def _run(cmd, *args, **kwargs):
        calls.append(cmd)
        # The output path is always the last element of the ffmpeg command
        # lists built in tts_tool.
        out_path = cmd[-1]
        Path(out_path).write_bytes(b"fake-encoded-audio")
        result = MagicMock()
        result.returncode = 0
        return result

    return _run


# ---------------------------------------------------------------------------
# Piper
# ---------------------------------------------------------------------------

class _StubPiperVoiceForCodec:
    @classmethod
    def load(cls, model_path, use_cuda=False):
        return cls()

    def synthesize_wav(self, text, wav_file, syn_config=None):
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 1024)


@pytest.fixture(autouse=True)
def _reset_piper_cache():
    tts_tool._piper_voice_cache.clear()
    yield
    tts_tool._piper_voice_cache.clear()


def _prepare_piper_voice(tmp_path):
    model = tmp_path / f"{tts_tool.DEFAULT_PIPER_VOICE}.onnx"
    model.write_bytes(b"model")
    (tmp_path / f"{tts_tool.DEFAULT_PIPER_VOICE}.onnx.json").write_text("{}")
    return model


def test_piper_ogg_output_forces_libopus(tmp_path, monkeypatch):
    model = _prepare_piper_voice(tmp_path)
    monkeypatch.setattr(tts_tool, "_import_piper", lambda: _StubPiperVoiceForCodec)
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_ffmpeg_run(calls))

    out_path = str(tmp_path / "clip.ogg")
    result = tts_tool._generate_piper_tts("hello", out_path, {"piper": {"voice": str(model)}})

    assert result == out_path
    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" in cmd and cmd[cmd.index("-acodec") + 1] == "libopus"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "64k"
    assert "-vbr" in cmd and cmd[cmd.index("-vbr") + 1] == "off"
    assert "-application" in cmd and cmd[cmd.index("-application") + 1] == "voip"


def test_piper_mp3_output_does_not_force_opus_flags(tmp_path, monkeypatch):
    model = _prepare_piper_voice(tmp_path)
    monkeypatch.setattr(tts_tool, "_import_piper", lambda: _StubPiperVoiceForCodec)
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_ffmpeg_run(calls))

    out_path = str(tmp_path / "clip.mp3")
    tts_tool._generate_piper_tts("hello", out_path, {"piper": {"voice": str(model)}})

    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" not in cmd
    assert "libopus" not in cmd


# ---------------------------------------------------------------------------
# KittenTTS
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_kittentts_cache():
    tts_tool._kittentts_model_cache.clear()
    yield
    tts_tool._kittentts_model_cache.clear()


@pytest.fixture
def _mock_kittentts_module(monkeypatch):
    fake_model = MagicMock()
    fake_model.generate.return_value = [0.0] * 48000
    fake_cls = MagicMock(return_value=fake_model)
    fake_kittentts = types.SimpleNamespace(KittenTTS=fake_cls)

    fake_sf = types.SimpleNamespace()

    def _fake_write(path, audio, samplerate):
        Path(path).write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

    fake_sf.write = _fake_write

    import sys
    monkeypatch.setitem(sys.modules, "kittentts", fake_kittentts)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    return fake_model


def test_kittentts_ogg_output_forces_libopus(tmp_path, monkeypatch, _mock_kittentts_module):
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_ffmpeg_run(calls))

    out_path = str(tmp_path / "clip.ogg")
    result = tts_tool._generate_kittentts("hello", out_path, {})

    assert result == out_path
    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" in cmd and cmd[cmd.index("-acodec") + 1] == "libopus"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "64k"
    assert "-vbr" in cmd and cmd[cmd.index("-vbr") + 1] == "off"
    assert "-application" in cmd and cmd[cmd.index("-application") + 1] == "voip"


def test_kittentts_mp3_output_does_not_force_opus_flags(tmp_path, monkeypatch, _mock_kittentts_module):
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_ffmpeg_run(calls))

    out_path = str(tmp_path / "clip.mp3")
    tts_tool._generate_kittentts("hello", out_path, {})

    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" not in cmd
    assert "libopus" not in cmd


# ---------------------------------------------------------------------------
# NeuTTS
# ---------------------------------------------------------------------------

def _fake_synth_and_ffmpeg_run(calls, wav_path_holder):
    """subprocess.run stand-in that handles both the neutts_synth.py
    subprocess call (writes the intermediate WAV) and the subsequent
    ffmpeg conversion call (records the command, writes the final file)."""

    def _run(cmd, *args, **kwargs):
        if cmd and "neutts_synth.py" in str(cmd[1]):
            # Synth call: `[sys.executable, synth_script, "--out", wav_path, ...]`
            out_idx = cmd.index("--out") + 1
            wav_path = cmd[out_idx]
            Path(wav_path).write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt fake")
            wav_path_holder.append(wav_path)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result
        else:
            calls.append(cmd)
            out_path = cmd[-1]
            Path(out_path).write_bytes(b"fake-encoded-audio")
            result = MagicMock()
            result.returncode = 0
            return result

    return _run


def test_neutts_ogg_output_forces_libopus(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    wav_holder = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_synth_and_ffmpeg_run(calls, wav_holder))

    out_path = str(tmp_path / "clip.ogg")
    result = tts_tool._generate_neutts("hello", out_path, {})

    assert result == out_path
    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" in cmd and cmd[cmd.index("-acodec") + 1] == "libopus"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "64k"
    assert "-vbr" in cmd and cmd[cmd.index("-vbr") + 1] == "off"
    assert "-application" in cmd and cmd[cmd.index("-application") + 1] == "voip"


def test_neutts_mp3_output_does_not_force_opus_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    calls = []
    wav_holder = []
    monkeypatch.setattr(tts_tool.subprocess, "run", _fake_synth_and_ffmpeg_run(calls, wav_holder))

    out_path = str(tmp_path / "clip.mp3")
    tts_tool._generate_neutts("hello", out_path, {})

    assert len(calls) == 1
    cmd = calls[0]
    assert "-acodec" not in cmd
    assert "libopus" not in cmd
