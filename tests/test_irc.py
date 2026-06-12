from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import open_strix.app as app_mod
from open_strix.irc import (
    IrcBridge,
    is_addressed,
    markdown_to_irc,
    parse_irc_line,
    split_irc_lines,
)


class DummyAgent:
    async def ainvoke(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"messages": []}


def _stub_agent_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "create_deep_agent", lambda **_: DummyAgent())


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_config: str = "") -> Any:
    _stub_agent_factory(monkeypatch)
    (tmp_path / "config.yaml").write_text(extra_config, encoding="utf-8")
    return app_mod.OpenStrixApp(tmp_path)


# ----------------------------------------------------------------------
# Pure functions
# ----------------------------------------------------------------------


def test_parse_irc_line_with_prefix_and_trailing() -> None:
    line = parse_irc_line(":nick!user@host PRIVMSG #chan :hello world\r\n")
    assert line.prefix == "nick!user@host"
    assert line.sender_nick == "nick"
    assert line.command == "PRIVMSG"
    assert line.params == ["#chan", "hello world"]


def test_parse_irc_line_without_prefix() -> None:
    line = parse_irc_line("PING :irc.example.org\r\n")
    assert line.prefix == ""
    assert line.command == "PING"
    assert line.params == ["irc.example.org"]


def test_parse_irc_line_strips_ircv3_tags() -> None:
    line = parse_irc_line("@time=2026-06-10T00:00:00Z :n!u@h PRIVMSG #c :hi\r\n")
    assert line.command == "PRIVMSG"
    assert line.params == ["#c", "hi"]


def test_parse_irc_line_numeric() -> None:
    line = parse_irc_line(":server 433 * mynick :Nickname is already in use.\r\n")
    assert line.command == "433"
    assert line.params == ["*", "mynick", "Nickname is already in use."]


def test_markdown_to_irc_bold_italic_code() -> None:
    out = markdown_to_irc("**bold** and *italic* and `code`")
    assert out == "\x02bold\x02 and \x1ditalic\x1d and code"


def test_markdown_to_irc_code_fence_and_link() -> None:
    out = markdown_to_irc("```python\nx = 1\n```\nsee [docs](https://example.org)")
    assert "```" not in out
    assert "x = 1" in out
    assert "docs (https://example.org)" in out


def test_markdown_to_irc_header() -> None:
    assert markdown_to_irc("# Title") == "\x02Title\x02"


def test_split_irc_lines_drops_blank_and_wraps_long_lines() -> None:
    long_word_line = "word " * 200  # ~1000 bytes
    lines = split_irc_lines(f"short\n\n{long_word_line}")
    assert lines[0] == "short"
    assert all(len(line.encode("utf-8")) <= 400 for line in lines)
    assert " ".join(lines[1:]).split() == long_word_line.split()


def test_split_irc_lines_hard_slices_unbreakable_multibyte_runs() -> None:
    blob = "ä" * 600  # 1200 UTF-8 bytes, no spaces
    lines = split_irc_lines(blob)
    assert all(len(line.encode("utf-8")) <= 400 for line in lines)
    assert "".join(lines) == blob


def test_is_addressed() -> None:
    assert is_addressed("strix", "strix: hello") is True
    assert is_addressed("strix", "strix, hello") is True
    assert is_addressed("strix", "hey strix what do you think") is True
    assert is_addressed("strix", "STRIX: caps too") is True
    assert is_addressed("strix", "the strixbot is offline") is False
    assert is_addressed("strix", "nothing relevant here") is False
    assert is_addressed("", "anything") is False


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------


def test_send_channel_message_routes_irc_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    async def fake_send_irc(**kwargs: Any) -> tuple[bool, str | None, int]:
        calls.append(kwargs)
        return True, "irc-abc", 1

    monkeypatch.setattr(app, "_send_irc_message", fake_send_irc)

    sent, message_id, chunks = asyncio.run(
        app._send_channel_message(channel_id="irc:#test", text="hello"),
    )

    assert sent is True
    assert message_id == "irc-abc"
    assert chunks == 1
    assert calls[0]["channel_id"] == "irc:#test"


def test_is_irc_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app(tmp_path, monkeypatch)
    assert app.is_irc_channel("irc:#chan") is True
    assert app.is_irc_channel("irc:somenick") is True
    assert app.is_irc_channel("123456789") is False
    assert app.is_irc_channel("local-web") is False
    assert app.is_irc_channel(None) is False


def test_send_irc_message_truncates_channel_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    sent_lines: list[str] = []

    class FakeBridge:
        connected = True
        current_nick = "strix"

        async def send_privmsg(self, target: str, lines: list[str]) -> int:
            sent_lines.extend(lines)
            return len(lines)

    app.irc_bridge = FakeBridge()
    text = "\n".join(f"line {i}" for i in range(30))

    sent, message_id, chunks = asyncio.run(
        app._send_irc_message(channel_id="irc:#test", text=text),
    )

    assert sent is True
    assert message_id is not None
    assert len(sent_lines) == 10
    assert "truncated for IRC" in sent_lines[-1]
    # History keeps the original markdown text.
    history = list(app.message_history_by_channel["irc:#test"])
    assert history[-1]["content"] == text
    assert history[-1]["source"] == "irc"


# ----------------------------------------------------------------------
# Inbound
# ----------------------------------------------------------------------


def test_irc_query_always_enqueues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app(tmp_path, monkeypatch)
    app.irc_bridge = SimpleNamespace(current_nick="strix", connected=True)

    asyncio.run(app.handle_irc_privmsg(sender_nick="alice", target="strix", text="hi there"))

    event = app.queue.get_nowait()
    assert event.event_type == "irc_message"
    assert event.channel_id == "irc:alice"
    assert event.channel_conversation_type == "dm"
    assert event.channel_visibility == "private"
    assert event.author == "alice"
    assert event.source_platform == "irc"


def test_irc_unaddressed_channel_message_remembered_not_enqueued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    app.irc_bridge = SimpleNamespace(current_nick="strix", connected=True)

    asyncio.run(
        app.handle_irc_privmsg(sender_nick="alice", target="#chat", text="random chatter"),
    )

    assert app.queue.empty()
    history = list(app.message_history_by_channel["irc:#chat"])
    assert history[-1]["content"] == "random chatter"
    assert history[-1]["author"] == "alice"


def test_irc_addressed_channel_message_enqueues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    app.irc_bridge = SimpleNamespace(current_nick="strix", connected=True)

    asyncio.run(
        app.handle_irc_privmsg(sender_nick="alice", target="#chat", text="strix: hello"),
    )

    event = app.queue.get_nowait()
    assert event.channel_id == "irc:#chat"
    assert event.channel_conversation_type == "multi_user"
    assert event.channel_visibility == "public"


def test_irc_respond_only_when_addressed_false_restores_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(
        tmp_path,
        monkeypatch,
        extra_config="irc_respond_only_when_addressed: false\n",
    )
    app.irc_bridge = SimpleNamespace(current_nick="strix", connected=True)

    asyncio.run(
        app.handle_irc_privmsg(sender_nick="alice", target="#chat", text="random chatter"),
    )

    assert not app.queue.empty()


def test_irc_message_updates_phone_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    app.irc_bridge = SimpleNamespace(current_nick="strix", connected=True)

    asyncio.run(app.handle_irc_privmsg(sender_nick="alice", target="strix", text="hi"))

    entry = app.phone_book.entries.get("irc:alice")
    assert entry is not None
    assert entry.name == "alice"


# ----------------------------------------------------------------------
# Reactions
# ----------------------------------------------------------------------


def test_react_to_message_on_irc_channel_applies_to_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    app._remember_message(
        channel_id="irc:#chat",
        author="alice",
        content="react to me",
        attachment_names=[],
        message_id="irc-feedbeef0001",
        is_bot=False,
        source="irc",
    )

    reacted = asyncio.run(
        app._react_to_message(channel_id="irc:#chat", message_id="irc-feedbeef0001", emoji="✅"),
    )

    assert reacted is True
    history = list(app.message_history_by_channel["irc:#chat"])
    assert history[-1]["reactions"] == ["✅"]


# ----------------------------------------------------------------------
# Handshake against a scripted fake server
# ----------------------------------------------------------------------


class FakeIrcServer:
    """Scriptable in-process IRC server speaking over a real socket."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port: int = 0
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list[str] = []
        self._connected = asyncio.Event()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._connected.set()

    async def wait_connected(self) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=5)

    async def expect(self, command: str) -> str:
        """Read lines until one starting with the given command arrives."""
        assert self.reader is not None
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), timeout=5)
            assert raw, f"connection closed while waiting for {command}"
            line = raw.decode("utf-8").rstrip("\r\n")
            self.received.append(line)
            if line.split(" ", 1)[0].upper() == command.upper():
                return line

    async def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write(line.encode("utf-8") + b"\r\n")
        await self.writer.drain()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


def _make_bridge(app: Any, port: int, password: str = "") -> IrcBridge:
    bridge = IrcBridge(app)
    bridge.server = "127.0.0.1"
    bridge.port = port
    bridge.tls = False
    bridge.password = password
    bridge.channels = ["#test"]
    bridge.configured_nick = "strix"
    bridge.current_nick = "strix"
    app.irc_bridge = bridge
    return bridge


@pytest.mark.asyncio
async def test_handshake_with_nick_collision_join_and_privmsg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    server = FakeIrcServer()
    await server.start()
    bridge = _make_bridge(app, server.port)
    task = asyncio.create_task(bridge._connect_and_run())
    try:
        await server.wait_connected()
        await server.expect("NICK")
        await server.expect("USER")
        await server.send(":srv 433 * strix :Nickname is already in use.")
        nick_line = await server.expect("NICK")
        assert nick_line == "NICK strix_"
        await server.send(":srv 001 strix_ :Welcome")
        join_line = await server.expect("JOIN")
        assert join_line == "JOIN #test"
        assert bridge.connected is True
        assert bridge.current_nick == "strix_"

        await server.send(":alice!a@host PRIVMSG strix_ :hello bot")
        event = await asyncio.wait_for(app.queue.get(), timeout=5)
        assert event.event_type == "irc_message"
        assert event.channel_id == "irc:alice"
        assert event.prompt == "hello bot"

        # Server PING is answered.
        await server.send("PING :srv")
        pong_line = await server.expect("PONG")
        assert pong_line == "PONG :srv"
    finally:
        await server.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ConnectionError, Exception):
            pass


@pytest.mark.asyncio
async def test_handshake_with_sasl_plain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(tmp_path, monkeypatch)
    server = FakeIrcServer()
    await server.start()
    bridge = _make_bridge(app, server.port, password="sekrit")
    task = asyncio.create_task(bridge._connect_and_run())
    try:
        await server.wait_connected()
        cap_line = await server.expect("CAP")
        assert cap_line == "CAP REQ :sasl"
        await server.expect("PASS")
        await server.expect("NICK")
        await server.expect("USER")
        await server.send(":srv CAP * ACK :sasl")
        auth_line = await server.expect("AUTHENTICATE")
        assert auth_line == "AUTHENTICATE PLAIN"
        await server.send("AUTHENTICATE +")
        payload_line = await server.expect("AUTHENTICATE")
        payload = base64.b64decode(payload_line.split(" ", 1)[1])
        assert payload == b"strix\0strix\0sekrit"
        await server.send(":srv 903 strix :SASL authentication successful")
        cap_end = await server.expect("CAP")
        assert cap_end == "CAP END"
        await server.send(":srv 001 strix :Welcome")
        await server.expect("JOIN")
        assert bridge.connected is True
    finally:
        await server.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ConnectionError, Exception):
            pass
