import json

import pytest

from bot.utils import telegram_monitor as monitor
from scripts.telegram_transport_watch import save_state


def observe(state, direct, relay, now=100):
    return monitor.update_state(state, {"direct": direct, "relay": relay}, now)


def test_debounce_and_unchanged_state_do_not_spam():
    state = observe({}, True, True)
    state = observe(state, True, True)
    assert state["pending"] == []
    state = observe(state, False, True)
    state = observe(state, True, True)
    assert state["pending"] == []
    for _ in range(10):
        state = observe(state, False, True)
    assert len(state["pending"]) == 1
    assert "резервный туннель" in state["pending"][0]["text"]


def test_initial_missing_backup_is_reported_after_two_checks():
    state = observe({}, True, False)
    assert state["pending"] == []
    state = observe(state, True, False)
    assert len(state["pending"]) == 1
    assert "запасного пути сейчас нет" in state["pending"][0]["text"]


def test_partial_initialization_does_not_lose_first_incident():
    state = observe({}, False, False)
    state = observe(state, False, True)
    assert state["pending"] == []
    state = observe(state, False, True)
    assert len(state["pending"]) == 1


def test_outage_and_recovery_survive_restart_and_deliver_in_order(tmp_path):
    path = tmp_path / "state.json"
    state = observe(observe({}, False, False), False, False, now=100)
    save_state(path, state)
    assert monitor.deliver_pending(state, lambda text: False, lambda value: save_state(path, value)) == 0
    state = json.loads(path.read_text())  # new process after restart
    state = observe(observe(state, True, True, now=200), True, True, now=260)
    save_state(path, state)
    messages = []
    def send(text):
        messages.append(text)
        return True
    assert monitor.deliver_pending(state, send, lambda value: save_state(path, value)) == 2
    assert "Оба соединения с Telegram недоступны" in messages[0]
    assert "восстановлено" in messages[1]
    assert json.loads(path.read_text())["pending"] == []
    assert path.stat().st_mode & 0o777 == 0o600
    assert monitor.deliver_pending(state, send, lambda value: None) == 0


def test_probe_each_route_independently(monkeypatch):
    calls = []
    def post(*args, relay, **kwargs):
        calls.append(relay)
        if relay is not None:
            raise OSError()
        return 200, b'{"ok":true}'
    monkeypatch.setenv("TELEGRAM_RELAY_HOST", "127.0.0.1")
    monkeypatch.setattr(monitor, "_https_post_form", post)
    assert monitor.probe_routes("test") == {"direct": True, "relay": False}
    assert len(calls) == 2 and calls[0] is None


@pytest.mark.parametrize("response", [(200, b'{"ok":false}'), (403, b'{"ok":false}'), (200, b'not json')])
def test_unsuccessful_telegram_response_is_not_delivery(monkeypatch, response):
    monkeypatch.setattr(monitor, "telegram_api_post_form", lambda *args, **kwargs: response)
    assert not monitor.send_alert("test", 1, "test")
