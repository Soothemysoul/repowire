"""Tests for _resolve_whisper_config env parsing."""
import pytest

from repowire.telegram.bot import _resolve_whisper_config


def test_resolve_returns_none_when_cli_missing(monkeypatch):
    monkeypatch.delenv("REPOWIRE_WHISPER_CLI", raising=False)
    monkeypatch.setenv("REPOWIRE_WHISPER_MODEL", "/tmp/model.bin")
    assert _resolve_whisper_config() is None


def test_resolve_returns_none_when_model_missing(monkeypatch):
    monkeypatch.setenv("REPOWIRE_WHISPER_CLI", "/tmp/whisper")
    monkeypatch.delenv("REPOWIRE_WHISPER_MODEL", raising=False)
    assert _resolve_whisper_config() is None


def test_resolve_returns_tuple_when_both_present(monkeypatch):
    monkeypatch.setenv("REPOWIRE_WHISPER_CLI", "/tmp/whisper")
    monkeypatch.setenv("REPOWIRE_WHISPER_MODEL", "/tmp/model.bin")
    monkeypatch.delenv("REPOWIRE_WHISPER_LANG", raising=False)
    cfg = _resolve_whisper_config()
    assert cfg == ("/tmp/whisper", "/tmp/model.bin", "ru")


def test_resolve_respects_lang_override(monkeypatch):
    monkeypatch.setenv("REPOWIRE_WHISPER_CLI", "/tmp/whisper")
    monkeypatch.setenv("REPOWIRE_WHISPER_MODEL", "/tmp/model.bin")
    monkeypatch.setenv("REPOWIRE_WHISPER_LANG", "en")
    cfg = _resolve_whisper_config()
    assert cfg == ("/tmp/whisper", "/tmp/model.bin", "en")
