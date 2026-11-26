import asyncio
import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates, SendMessage

from bot.utils import telegram_relay as tr


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_DIRECT_HOST", "149.154.167.220")
    monkeypatch.setenv("TELEGRAM_RELAY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_RELAY_PORT", "18443")
    monkeypatch.setenv("TELEGRAM_RELAY_MODE", "fallback")
    monkeypatch.setenv("TELEGRAM_DIRECT_FAILS_BEFORE_SKIP", "1")
    monkeypatch.setenv("TELEGRAM_DIRECT_COOLDOWN_SEC", "60")
    monkeypatch.setenv("TELEGRAM_TRANSPORT_JOURNAL", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("TELEGRAM_TRANSPORT_SUMMARY", str(tmp_path / "summary.txt"))
    monkeypatch.setattr(tr, "_circuit", tr._DirectCircuit())
    monkeypatch.setattr(tr, "_stats", tr._TransportStats())


def test_pin_is_scoped_and_keeps_hostname():
    async def run():
        resolver = tr.TelegramRelayResolver(tr.telegram_direct_settings())
        other = [{"host": "192.0.2.1"}]
        resolver._default = SimpleNamespace(resolve=AsyncMock(return_value=other), close=AsyncMock())
        result = await resolver.resolve("api.telegram.org", 443)
        assert result[0]["host"] == "149.154.167.220"
        assert result[0]["hostname"] == "api.telegram.org"
        assert result[0]["port"] == 443
        assert await resolver.resolve("example.org", 443) == other
        assert await resolver.resolve("api.telegram.org", 80) == other
        assert resolver._default.resolve.await_count == 2
        await resolver.close()
    asyncio.run(run())


def test_sync_pin_keeps_tls_sni_and_validation(monkeypatch):
    raw = Mock()
    connect = Mock(return_value=raw)
    monkeypatch.setattr(tr.socket, "create_connection", connect)
    context = ssl.create_default_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    context.wrap_socket = Mock(return_value=Mock())
    connection = tr._TelegramRelayHTTPSConnection(
        tr.TELEGRAM_API_HOST, relay=tr.telegram_direct_settings(), timeout=5, context=context,
    )
    connection.connect()
    assert connect.call_args.args[0] == ("149.154.167.220", 443)
    context.wrap_socket.assert_called_once_with(raw, server_hostname="api.telegram.org")
    connection.close()


@pytest.mark.parametrize("method,error,expected", [
    ("sendMessage", tr.TelegramConnectError("refused"), True),
    ("sendPhoto", tr.TelegramConnectError("TLS failed"), True),
    ("sendMessage", TimeoutError("response lost"), False),
    ("sendPhoto", aiohttp.ServerDisconnectedError(), False),
    ("getMe", TimeoutError(), True),
    ("getUpdates", TimeoutError(), True),
])
def test_retry_requires_known_safe_request(method, error, expected):
    assert tr.safe_to_retry(method, error) is expected


def test_sync_failover_cooldown_and_recovery(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tr.time, "monotonic", lambda: now[0])
    calls = []
    primary_up = [False]

    def post(*args, relay, **kwargs):
        calls.append("relay" if relay else "direct")
        if relay is None and not primary_up[0]:
            raise tr.TelegramConnectError("unreachable")
        return 200, b'{"ok":true}'

    monkeypatch.setattr(tr, "_https_post_form", post)
    tr.telegram_api_post_form("test", "sendMessage", {})
    tr.telegram_api_post_form("test", "sendMessage", {})
    assert calls == ["direct", "relay", "relay"]
    primary_up[0] = True
    now[0] = 161
    tr.telegram_api_post_form("test", "sendMessage", {})
    assert calls[-1] == "direct"
    assert not tr._circuit.using_relay


def test_sync_ambiguous_send_is_not_replayed(monkeypatch):
    post = Mock(side_effect=TimeoutError("response lost"))
    monkeypatch.setattr(tr, "_https_post_form", post)
    with pytest.raises(TimeoutError):
        tr.telegram_api_post_form("test", "sendMessage", {})
    assert post.call_count == 1
    assert not tr._circuit.should_try_direct()


def test_sync_connect_error_is_classified_before_request(monkeypatch):
    conn = Mock()
    conn.connect.side_effect = OSError("unreachable")
    monkeypatch.setattr(tr, "_TelegramRelayHTTPSConnection", Mock(return_value=conn))
    with pytest.raises(tr.TelegramConnectError):
        tr._https_post_form("test", "sendMessage", {}, timeout=5, relay=None)
    conn.request.assert_not_called()
    conn.close.assert_called_once()


@pytest.mark.parametrize("method,error,retried", [
    (GetMe(), TimeoutError(), True),
    (SendMessage(chat_id=1, text="test"), TimeoutError(), False),
    (SendMessage(chat_id=1, text="test"), tr.TelegramConnectError("refused"), True),
])
def test_aiogram_retry_preserves_delivery_safety(monkeypatch, method, error, retried):
    async def run():
        wrapped = TelegramNetworkError(method=method, message="network")
        wrapped.original = error
        direct = SimpleNamespace(make_request=AsyncMock(side_effect=wrapped), close=AsyncMock())
        relay = SimpleNamespace(make_request=AsyncMock(return_value="ok"), close=AsyncMock())
        monkeypatch.setattr(tr, "_make_aiogram_session", lambda *, relay: relay_session if relay else direct)
        relay_session = relay
        session = tr._new_fallback_session()
        if retried:
            assert await session.make_request(None, method) == "ok"
            relay.make_request.assert_awaited_once()
        else:
            with pytest.raises(TelegramNetworkError):
                await session.make_request(None, method)
            relay.make_request.assert_not_awaited()
        await session.close()
    asyncio.run(run())


def test_aiogram_real_aiohttp_accepts_bounded_timeout(monkeypatch):
    # Exercise actual aiohttp request and aiogram response parsing without Telegram.
    from aiohttp import web
    from aiogram.client.telegram import TelegramAPIServer

    async def run():
        app = web.Application()
        async def get_updates(request):
            return web.json_response({"ok": True, "result": []})
        app.router.add_post("/bot{token}/getUpdates", get_updates)
        runner = web.AppRunner(app)
        await runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        session = tr._make_aiogram_session(relay=False)
        session.api = TelegramAPIServer.from_base(f"http://127.0.0.1:{port}")
        bot = Bot("123:test", session=session)
        try:
            assert await session.make_request(bot, GetUpdates(), timeout=tr._direct_timeout(2)) == []
            assert await session.make_request(bot, GetUpdates(), timeout=2) == []
        finally:
            await bot.session.close()
            await runner.cleanup()
    asyncio.run(run())


def test_partial_download_never_restarts_in_same_stream(monkeypatch):
    async def run():
        called = []
        async def direct_stream(*args, **kwargs):
            yield b"first chunk"
            raise TimeoutError()
        async def relay_stream(*args, **kwargs):
            called.append("relay")
            yield b"first chunk"
        direct = SimpleNamespace(stream_content=direct_stream, close=AsyncMock())
        relay = SimpleNamespace(stream_content=relay_stream, close=AsyncMock())
        monkeypatch.setattr(tr, "_make_aiogram_session", lambda **kw: relay if kw["relay"] else direct)
        session = tr._new_fallback_session()
        chunks = []
        with pytest.raises(TimeoutError):
            async for chunk in session.stream_content("https://api.telegram.org/file/test"):
                chunks.append(chunk)
        assert chunks == [b"first chunk"] and called == []
        await session.close()
    asyncio.run(run())


@pytest.mark.parametrize("method,retries", [("sendMessage", 1), ("getMe", 2)])
def test_json_timeout_does_not_duplicate_send(monkeypatch, method, retries):
    calls = []
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            calls.append(args)
            raise TimeoutError()
    monkeypatch.setattr(tr.aiohttp, "ClientSession", lambda **kwargs: FakeSession())
    monkeypatch.setattr(tr, "telegram_aiohttp_connector", lambda **kwargs: None)
    with pytest.raises(TimeoutError):
        asyncio.run(tr.telegram_aiohttp_post_json(f"https://api.telegram.org/bot123:test/{method}", json_body={}))
    assert len(calls) == retries
