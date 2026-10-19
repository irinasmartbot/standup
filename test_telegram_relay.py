import json
import os
import tempfile
from pathlib import Path

os.environ.pop("TELEGRAM_RELAY_HOST", None)
os.environ.pop("TELEGRAM_RELAY_MODE", None)
os.environ.pop("TELEGRAM_TRANSPORT_JOURNAL", None)

from bot.utils import telegram_relay as tr


def test_mode_off_without_host():
    os.environ.pop("TELEGRAM_RELAY_HOST", None)
    assert tr.telegram_relay_mode() == "off"


def test_mode_fallback_default_with_host():
    os.environ["TELEGRAM_RELAY_HOST"] = "127.0.0.1"
    os.environ["TELEGRAM_RELAY_PORT"] = "18443"
    os.environ.pop("TELEGRAM_RELAY_MODE", None)
    assert tr.telegram_relay_mode() == "fallback"
    os.environ["TELEGRAM_RELAY_MODE"] = "always"
    assert tr.telegram_relay_mode() == "always"


def test_sanitize_and_journal(tmp_path: Path):
    os.environ["TELEGRAM_TRANSPORT_JOURNAL"] = str(tmp_path / "journal.jsonl")
    tr.log_transport_event(
        "direct_fail",
        method="sendMessage",
        source="test",
        error="https://api.telegram.org/bot123456:ABC-SECRET/sendMessage timeout",
    )
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
    row = json.loads(lines[-1])
    assert row["event"] == "direct_fail"
    assert "ABC-SECRET" not in row["error"]
    assert "bot<redacted>" in row["error"]


def test_summary_is_readable(tmp_path: Path):
    os.environ["TELEGRAM_TRANSPORT_JOURNAL"] = str(tmp_path / "journal.jsonl")
    os.environ["TELEGRAM_TRANSPORT_SUMMARY"] = str(tmp_path / "summary.txt")
    tr._stats = tr._TransportStats()
    tr.log_transport_event("direct_ok", method="getMe", source="test")
    tr.log_transport_event("direct_fail", method="sendMessage", source="test", error="timeout")
    tr.log_transport_event("relay_ok", method="sendMessage", source="test")
    text = (tmp_path / "summary.txt").read_text(encoding="utf-8")
    assert "через мост" in text
    assert "напрямую" in text
    assert "timeout" in text
    assert "ABC-SECRET" not in text


def test_circuit_opens_after_fails():
    os.environ["TELEGRAM_DIRECT_FAILS_BEFORE_SKIP"] = "3"
    os.environ["TELEGRAM_DIRECT_COOLDOWN_SEC"] = "60"
    circuit = tr._DirectCircuit()
    tr._circuit = circuit
    assert circuit.should_try_direct()
    for _ in range(3):
        circuit.on_fail("getMe", "test", OSError("blocked"))
    assert circuit.should_try_direct() is False
    circuit.skip_until = 0
    assert circuit.should_try_direct() is True


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_mode_off_without_host()
        test_mode_fallback_default_with_host()
        test_sanitize_and_journal(tmp)
        test_summary_is_readable(tmp)
        test_circuit_opens_after_fails()
        print("ok")
