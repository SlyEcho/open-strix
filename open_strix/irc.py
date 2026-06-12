"""IRC transport: a thin asyncio IRC client bridge plus the app-facing mixin.

Mirrors the shape of the Discord transport (`DiscordBridge` + `DiscordMixin`)
and the web transport (`WebChatMixin`).  Channel ids are namespaced with an
``irc:`` prefix (``irc:#chan`` for channels, ``irc:nick`` for queries) so they
never collide with Discord's numeric ids or the web UI channel id.

IRC has no message ids, reactions, attachments, or markdown; those degrade the
same way the web transport degrades them: synthesized message ids, memory-only
reactions, attachment names as text, and a lossy markdown-to-IRC conversion.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import ssl as ssl_module
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from .models import AgentEvent
from .phone_book import save_phone_book, update_from_message

LOGGER = logging.getLogger(__name__)

IRC_CHANNEL_PREFIX = "irc:"
# 512-byte PRIVMSG budget minus ":nick!user@host PRIVMSG #target :" overhead.
IRC_LINE_MAX_BYTES = 400
IRC_CHANNEL_LINE_CAP = 10
IRC_QUERY_LINE_CAP = 30
IRC_RECONNECT_MAX_DELAY = 60
IRC_STABLE_CONNECTION_SECONDS = 300
IRC_PING_AFTER_SILENCE_SECONDS = 90
IRC_DEAD_AFTER_SILENCE_SECONDS = 240

_BOLD = "\x02"
_ITALIC = "\x1d"
_CTCP = "\x01"


# ----------------------------------------------------------------------
# Line parsing
# ----------------------------------------------------------------------


@dataclass
class IrcLine:
    prefix: str = ""
    command: str = ""
    params: list[str] = field(default_factory=list)

    @property
    def sender_nick(self) -> str:
        return self.prefix.split("!", 1)[0]


def parse_irc_line(raw: str) -> IrcLine:
    """Parse one raw IRC line into prefix, command, and params (incl. trailing)."""
    line = raw.rstrip("\r\n")
    if line.startswith("@"):
        # IRCv3 message tags — we don't use them; strip.
        _, _, line = line.partition(" ")
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing: str | None = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    params = line.split()
    command = params.pop(0).upper() if params else ""
    if trailing is not None:
        params.append(trailing)
    return IrcLine(prefix=prefix, command=command, params=params)


# ----------------------------------------------------------------------
# Outbound formatting
# ----------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL | re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def markdown_to_irc(text: str) -> str:
    """Lossy markdown → IRC-formatted plain text.  When unsure, pass through."""

    def _fence(match: re.Match[str]) -> str:
        return match.group(1)

    out = re.sub(r"```[^\n]*\n?(.*?)\n?```", _fence, text, flags=re.DOTALL)
    out = _HEADER_RE.sub(lambda m: f"{_BOLD}{m.group(1)}{_BOLD}", out)
    out = _BOLD_RE.sub(lambda m: f"{_BOLD}{m.group(1) or m.group(2)}{_BOLD}", out)
    out = _ITALIC_RE.sub(lambda m: f"{_ITALIC}{m.group(1) or m.group(2)}{_ITALIC}", out)
    out = _INLINE_CODE_RE.sub(lambda m: m.group(1), out)

    def _link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        return url if label == url else f"{label} ({url})"

    out = _LINK_RE.sub(_link, out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def split_irc_lines(text: str, max_bytes: int = IRC_LINE_MAX_BYTES) -> list[str]:
    """Split text into sendable IRC lines: newline-split, then UTF-8 word-wrap."""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if len(line.encode("utf-8")) <= max_bytes:
            lines.append(line)
            continue
        current = ""
        for word in line.split(" "):
            candidate = f"{current} {word}" if current else word
            if len(candidate.encode("utf-8")) <= max_bytes:
                current = candidate
                continue
            if current:
                lines.append(current)
            # Hard-slice unbreakable runs longer than the budget.
            while len(word.encode("utf-8")) > max_bytes:
                encoded = word.encode("utf-8")
                cut = max_bytes
                while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
                    cut -= 1
                lines.append(encoded[:cut].decode("utf-8"))
                word = encoded[cut:].decode("utf-8")
            current = word
        if current:
            lines.append(current)
    return lines


def is_addressed(nick: str, text: str) -> bool:
    """True when an IRC channel message addresses the given nick."""
    if not nick:
        return False
    escaped = re.escape(nick)
    if re.match(rf"^\s*{escaped}\s*[:,]", text, re.IGNORECASE):
        return True
    return bool(re.search(rf"(?<![\w\[\]{{}}^`|-]){escaped}(?![\w\[\]{{}}^`|-])", text, re.IGNORECASE))


# ----------------------------------------------------------------------
# Bridge
# ----------------------------------------------------------------------


class IrcBridge:
    """Minimal asyncio IRC client driving the app's IrcMixin."""

    def __init__(self, app: Any) -> None:
        self._app = app
        config = app.config
        self.server: str = config.irc_server
        self.port: int = config.irc_port
        self.tls: bool = config.irc_tls
        self.configured_nick: str = config.irc_nick or config.name or "open-strix"
        self.channels: list[str] = list(config.irc_channels)
        self.password: str = os.getenv(config.irc_password_env, "")
        self.current_nick: str = self.configured_nick
        self.connected: bool = False
        self._stop = False
        self._writer: asyncio.StreamWriter | None = None
        self._nick_attempts = 0
        self._send_bucket = 4.0
        self._send_bucket_ts = 0.0

    # -- lifecycle -----------------------------------------------------

    async def run_forever(self) -> None:
        attempt = 0
        loop = asyncio.get_running_loop()
        while not self._stop:
            started = loop.time()
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._app.log_event("irc_error", error=str(exc))
            finally:
                self.connected = False
                self._writer = None
            if self._stop:
                break
            if loop.time() - started > IRC_STABLE_CONNECTION_SECONDS:
                attempt = 0
            delay = min(IRC_RECONNECT_MAX_DELAY, 2**attempt)
            attempt += 1
            self._app.log_event("irc_reconnecting", delay_seconds=delay)
            await asyncio.sleep(delay)

    async def close(self) -> None:
        self._stop = True
        writer = self._writer
        if writer is not None:
            try:
                writer.write(b"QUIT :shutting down\r\n")
                await writer.drain()
                writer.close()
            except Exception:
                pass
        self._writer = None
        self.connected = False

    # -- connection ----------------------------------------------------

    async def _connect_and_run(self) -> None:
        ssl_context = ssl_module.create_default_context() if self.tls else None
        reader, writer = await asyncio.open_connection(
            self.server, self.port, ssl=ssl_context,
        )
        self._writer = writer
        self.current_nick = self.configured_nick
        self._nick_attempts = 0
        try:
            await self._register()
            await self._read_loop(reader)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _register(self) -> None:
        if self.password:
            # CAP REQ before NICK holds registration until CAP END; SASL
            # outcome (903/904) drives the CAP END in _handle_line.  Ancient
            # servers without CAP reply 421 and register anyway.  PASS covers
            # bouncers/servers using a plain server password; SASL-capable
            # servers ignore it.
            await self._send_raw("CAP REQ :sasl")
            await self._send_raw(f"PASS {self.password}")
        await self._send_raw(f"NICK {self.current_nick}")
        await self._send_raw(f"USER {self.configured_nick} 0 * :open-strix")

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        silence = 0.0
        while not self._stop:
            try:
                raw = await asyncio.wait_for(
                    reader.readline(), timeout=IRC_PING_AFTER_SILENCE_SECONDS,
                )
            except asyncio.TimeoutError:
                silence += IRC_PING_AFTER_SILENCE_SECONDS
                if silence >= IRC_DEAD_AFTER_SILENCE_SECONDS:
                    raise ConnectionError("IRC connection silent; reconnecting")
                await self._send_raw(f"PING :{self.server}")
                continue
            if not raw:
                if self._stop:
                    return
                raise ConnectionError("IRC connection closed by server")
            silence = 0.0
            line = parse_irc_line(raw.decode("utf-8", errors="replace"))
            await self._handle_line(line)

    async def _handle_line(self, line: IrcLine) -> None:
        command = line.command
        if command == "PING":
            payload = line.params[-1] if line.params else self.server
            await self._send_raw(f"PONG :{payload}")
        elif command == "CAP":
            await self._handle_cap(line)
        elif command == "AUTHENTICATE":
            if line.params and line.params[-1] == "+":
                # SASL PLAIN payload: authzid NUL authcid NUL password.
                payload = f"{self.configured_nick}\0{self.configured_nick}\0{self.password}"
                encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
                await self._send_raw(f"AUTHENTICATE {encoded}")
        elif command == "903":  # SASL success
            self._app.log_event("irc_sasl_ok")
            await self._send_raw("CAP END")
        elif command in ("904", "905", "906", "908"):  # SASL failure variants
            self._app.log_event("irc_sasl_failed", code=command)
            await self._send_raw("CAP END")
        elif command == "433":  # nick in use
            self._nick_attempts += 1
            if self._nick_attempts <= 3:
                self.current_nick = f"{self.current_nick}_"
            else:
                self.current_nick = f"{self.configured_nick}{random.randint(100, 999)}"
            await self._send_raw(f"NICK {self.current_nick}")
        elif command == "001":  # welcome
            self.connected = True
            self._app.log_event("irc_ready", nick=self.current_nick, server=self.server)
            print(
                f"IRC connected to {self.server} as {self.current_nick}; "
                f"joining {', '.join(self.channels) or '(no channels)'}",
                flush=True,
            )
            for channel in self.channels:
                await self._send_raw(f"JOIN {channel}")
        elif command == "NICK" and line.sender_nick == self.current_nick:
            if line.params:
                self.current_nick = line.params[-1]
        elif command == "PRIVMSG":
            await self._handle_privmsg(line)
        elif command == "NOTICE":
            self._handle_notice(line)

    async def _handle_cap(self, line: IrcLine) -> None:
        subcommand = line.params[1].upper() if len(line.params) > 1 else ""
        if subcommand == "ACK" and "sasl" in line.params[-1].lower():
            await self._send_raw("AUTHENTICATE PLAIN")
        elif subcommand == "NAK":
            self._app.log_event("irc_sasl_unavailable")
            await self._send_raw("CAP END")

    async def _handle_privmsg(self, line: IrcLine) -> None:
        if len(line.params) < 2:
            return
        sender = line.sender_nick
        target, text = line.params[0], line.params[1]
        if sender == self.current_nick:
            return
        if text.startswith(_CTCP) and text.endswith(_CTCP):
            ctcp = text[1:-1]
            if ctcp.startswith("ACTION "):
                text = f"* {sender} {ctcp[len('ACTION '):]}"
            elif ctcp == "VERSION":
                await self._send_raw(f"NOTICE {sender} :{_CTCP}VERSION open-strix{_CTCP}")
                return
            elif ctcp.startswith("PING"):
                await self._send_raw(f"NOTICE {sender} :{_CTCP}{ctcp}{_CTCP}")
                return
            else:
                return
        await self._app.handle_irc_privmsg(sender_nick=sender, target=target, text=text)

    def _handle_notice(self, line: IrcLine) -> None:
        # RFC: never auto-respond to NOTICE.  Remember user notices in history.
        if "!" not in line.prefix or len(line.params) < 2:
            return
        sender = line.sender_nick
        target, text = line.params[0], line.params[1]
        if text.startswith(_CTCP):
            return
        channel_id = (
            f"{IRC_CHANNEL_PREFIX}{target}"
            if target.startswith(("#", "&"))
            else f"{IRC_CHANNEL_PREFIX}{sender}"
        )
        self._app._remember_message(
            channel_id=channel_id,
            author=sender,
            content=text,
            attachment_names=[],
            message_id=self._app._new_irc_message_id(),
            is_bot=False,
            source="irc",
        )

    # -- sending -------------------------------------------------------

    async def _send_raw(self, line: str) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("IRC writer not available")
        writer.write(line.encode("utf-8") + b"\r\n")
        await writer.drain()

    async def _throttle(self) -> None:
        """Token bucket: burst of 4 lines, then 1 line/second."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._send_bucket = min(4.0, self._send_bucket + (now - self._send_bucket_ts))
        self._send_bucket_ts = now
        if self._send_bucket < 1.0:
            await asyncio.sleep(1.0 - self._send_bucket)
            self._send_bucket_ts = loop.time()
            self._send_bucket = 0.0
        else:
            self._send_bucket -= 1.0

    async def send_privmsg(self, target: str, lines: list[str]) -> int:
        sent = 0
        for line in lines:
            await self._throttle()
            await self._send_raw(f"PRIVMSG {target} :{line}")
            sent += 1
        return sent


# ----------------------------------------------------------------------
# Mixin
# ----------------------------------------------------------------------


class IrcMixin:
    def is_irc_channel(self, channel_id: str | None) -> bool:
        if channel_id in (None, ""):
            return False
        return str(channel_id).strip().startswith(IRC_CHANNEL_PREFIX)

    def _new_irc_message_id(self) -> str:
        return f"irc-{uuid4().hex[:12]}"

    @staticmethod
    def _irc_target(channel_id: str) -> str:
        return str(channel_id).strip()[len(IRC_CHANNEL_PREFIX):]

    async def _send_irc_message(
        self,
        *,
        channel_id: str,
        text: str,
        attachment_names: list[str] | None = None,
        format: str = "markdown",
    ) -> tuple[bool, str | None, int]:
        target = self._irc_target(channel_id)
        outbound_attachment_names = attachment_names or []
        body = markdown_to_irc(text)
        if outbound_attachment_names:
            body = f"{body}\n[attachments: {', '.join(outbound_attachment_names)}]"
        lines = split_irc_lines(body)
        cap = IRC_CHANNEL_LINE_CAP if target.startswith(("#", "&")) else IRC_QUERY_LINE_CAP
        if len(lines) > cap:
            truncated = len(lines) - (cap - 1)
            lines = lines[: cap - 1]
            lines.append(f"… (reply truncated for IRC; {truncated} more lines)")

        bridge: IrcBridge | None = getattr(self, "irc_bridge", None)
        if bridge is None or not bridge.connected:
            for line in lines:
                print(f"[open-strix send_message channel={channel_id}] {line}")
            return False, None, len(lines)

        sent_lines = await bridge.send_privmsg(target, lines)
        message_id = self._new_irc_message_id()
        # History keeps the original markdown; the IRC conversion is lossy.
        self._remember_message(
            channel_id=channel_id,
            author="open_strix",
            content=text,
            attachment_names=outbound_attachment_names,
            message_id=message_id,
            is_bot=True,
            source="irc",
            format=format,
        )
        if self._current_turn_sent_messages is not None:
            self._current_turn_sent_messages.append((channel_id, message_id))
        return True, message_id, sent_lines

    async def _react_to_irc_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        # IRC has no reactions; record them in memory only, like the web UI.
        return self._apply_reaction_to_memory(
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )

    async def handle_irc_privmsg(self, *, sender_nick: str, target: str, text: str) -> None:
        is_channel = target.startswith(("#", "&"))
        if is_channel:
            channel_id = f"{IRC_CHANNEL_PREFIX}{target}"
            channel_name = target
            conversation_type = "multi_user"
            visibility = "public"
        else:
            channel_id = f"{IRC_CHANNEL_PREFIX}{sender_nick}"
            channel_name = sender_nick
            conversation_type = "dm"
            visibility = "private"

        message_id = self._new_irc_message_id()
        author_id = f"{IRC_CHANNEL_PREFIX}{sender_nick}"

        author = SimpleNamespace(id=author_id, display_name=sender_nick, bot=False)
        if update_from_message(self.phone_book, author):
            save_phone_book(self.phone_book, self.layout.phone_book_file)

        # Full channel context goes into history even when we don't respond.
        self._remember_message(
            channel_id=channel_id,
            author=sender_nick,
            content=text,
            attachment_names=[],
            message_id=message_id,
            is_bot=False,
            source="irc",
        )

        bridge: IrcBridge | None = getattr(self, "irc_bridge", None)
        own_nick = bridge.current_nick if bridge is not None else ""
        if (
            is_channel
            and self.config.irc_respond_only_when_addressed
            and not is_addressed(own_nick, text)
        ):
            return

        prompt = text.strip() or "User sent a message with no text."
        self.log_event(
            "irc_message",
            channel_id=channel_id,
            author=sender_nick,
            author_id=author_id,
            channel_name=channel_name,
            channel_conversation_type=conversation_type,
            channel_visibility=visibility,
            source_id=message_id,
            content=prompt,
        )
        await self.enqueue_event(
            AgentEvent(
                event_type="irc_message",
                prompt=prompt,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_conversation_type=conversation_type,
                channel_visibility=visibility,
                author=sender_nick,
                author_id=author_id,
                source_id=message_id,
                source_platform="irc",
            ),
        )
