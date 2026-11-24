"""Signal destination: posts through signal-cli-rest-api, over HTTP.

signal-cli-rest-api (https://github.com/bbernhard/signal-cli-rest-api) runs
as its own container, with a Signal account registered or linked in it.
SIGNAL_SENDER is that account's number and SIGNAL_API_URL is where the API
answers. This module only speaks that HTTP API, with the standard library,
so the image needs nothing extra.

How messages map:
* Text and captions go as plain text (text_mode "normal"): quote blocks
  become "> " lines, a link behind words shows its URL after them, and other
  formatting is dropped. (The API's "styled" mode needs its own escaping,
  not done yet.)
* Media goes as one attachment with its caption. Voice notes arrive as audio
  files, GIFs as videos and stickers as images.
* A reply quotes the earlier message when that one was forwarded here too.
  Signal refers to a message by its timestamp, which /v2/send returns and
  the pipeline stores like any other message id.
* The API has no polls, so send_poll() returns None and the pipeline posts
  a numbered list instead.
* Edits apply to text messages only: an edit can't re-attach the file.
* If the API doesn't answer, the account isn't registered in it or the
  recipient is wrong, the destination is reported Unreachable: the bridge
  pauses it and checks again every minute while other destinations go on.
"""
import asyncio
import base64
import http.client
import json
import mimetypes
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Tuple

from ...core import rich as fmt
from ...core.model import Dest, Media, Poll
from ...core.sink import CantEdit, Rejected, Unreachable

NAME = "Signal"
TEXT_LIMIT = 2000                   # Signal apps cut longer messages short
MAX_ATTACHMENT = 100 * 1024 * 1024  # Signal's attachment limit
SEND_TIMEOUT_S = 120                # "normal" mode starts a JVM per request; uploads take time
CHECK_TIMEOUT_S = 30
RETRY_DELAY_S = 2
TOO_BIG_NOTE = "⚠️ The attachment is larger than Signal allows (100 MB), so it was left out."

# 400 answers that mean the destination or account is set up wrong, rather
# than that this one message was refused.
_SETUP_ERRORS = (
    "account does not exist", "not registered", "unregistered", "invalid identifier",
    "invalid group", "group not found", "untrusted identity",
)
_B64_BLOCK = 3 * 64 * 1024  # a multiple of 3, so base64 has no padding until the end

# (method, url, body or None, headers, timeout) -> (HTTP status, response body)
Transport = Callable[[str, str, Optional[BinaryIO], dict, float], Tuple[int, bytes]]

# A local service: never send it through an HTTP proxy set in the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urllib_transport(method: str, url: str, body: Optional[BinaryIO], headers: dict,
                      timeout: float) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class SignalSink:
    name = NAME

    def __init__(self, base_url: str, sender: str, transport: Transport = _urllib_transport):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.sender = (sender or "").strip()
        self._transport = transport
        self.retry_delay = RETRY_DELAY_S
        self.max_attachment = MAX_ATTACHMENT

    async def start(self) -> None:
        if not self.sender:
            raise SystemExit("Signal destinations need SIGNAL_SENDER in .env: the number (with + and "
                             "country code) of the Signal account set up in signal-cli-rest-api.")
        if not self.base_url:
            raise SystemExit("SIGNAL_API_URL is empty; set it in .env, e.g. http://signal-api:8080.")

    async def stop(self) -> None:
        pass

    async def resolve(self, dest: Dest) -> str:
        accounts = await self._get("/v1/accounts")
        known = set()
        for account in accounts if isinstance(accounts, list) else []:
            if isinstance(account, str):  # older versions list bare numbers
                known.add(account)
            elif isinstance(account, dict):
                known.update(str(account.get(k) or "") for k in ("number", "aci"))
        if self.sender not in known:
            raise Unreachable(f"{self.sender} isn't registered or linked in signal-cli-rest-api", NAME,
                              hint="Link or register it there (README §12, Signal destinations)")
        if dest.chat.startswith("group."):
            groups = await self._get(f"/v1/groups/{urllib.parse.quote(self.sender, safe='+')}")
            ids = {g.get("id") for g in groups if isinstance(g, dict)} if isinstance(groups, list) else set()
            if dest.chat not in ids:
                raise Unreachable(f"the Signal account isn't a member of group {dest.chat}", NAME,
                                  hint="Add it to the group, or check the id with /v1/groups")
        return dest.chat

    # ------------------------------------------------------------------ posting

    async def send_text(self, dest: Dest, text: fmt.Rich, reply_to: Optional[int]) -> Optional[int]:
        first = None
        for chunk in fmt.split(as_plain(text), TEXT_LIMIT):
            sent = await self._send(dest, chunk.text, quote=reply_to if first is None else None)
            if first is None:
                first = sent
        return first

    async def send_media(self, dest: Dest, media: Media, caption: fmt.Rich, path: Path,
                         reply_to: Optional[int]) -> Optional[int]:
        caption = as_plain(caption)
        if path.stat().st_size > self.max_attachment:
            gap = fmt.plain("\n\n" if caption.text else "")
            return await self.send_text(dest, fmt.concat(caption, gap, fmt.plain(TOO_BIG_NOTE)), reply_to)
        chunks = fmt.split(caption, TEXT_LIMIT) if caption.text else []
        sent = await self._send(dest, chunks[0].text if chunks else "", quote=reply_to,
                                attachment=(media, path))
        for chunk in chunks[1:]:  # caption longer than Signal shows
            await self._send(dest, chunk.text)
        return sent

    async def send_poll(self, dest: Dest, poll: Poll, reply_to: Optional[int]) -> Optional[int]:
        return None  # no polls in the API: the pipeline posts a numbered list

    async def edit(self, dest: Dest, msg_id: int, text: fmt.Rich, is_media: bool) -> bool:
        if is_media:
            raise CantEdit("an edit from here can't keep the attachment, so captions stay as sent", NAME)
        if not msg_id:
            raise CantEdit("the original's Signal timestamp isn't known", NAME)
        try:
            await self._send(dest, fmt.truncate(as_plain(text), TEXT_LIMIT).text, edit=msg_id)
        except Rejected as e:
            raise CantEdit(str(e), NAME) from e
        return True

    async def _send(self, dest: Dest, text: str, *, quote: Optional[int] = None,
                    edit: Optional[int] = None,
                    attachment: Optional[Tuple[Media, Path]] = None) -> int:
        fields = {"message": text, "number": self.sender, "recipients": [dest.chat],
                  "text_mode": "normal"}
        if quote:
            fields["quote_timestamp"] = int(quote)
            fields["quote_author"] = self.sender  # we posted the original
        if edit:
            fields["edit_timestamp"] = int(edit)
        status, body = await self._request("POST", "/v2/send", fields, attachment, SEND_TIMEOUT_S)
        if status not in (200, 201):
            raise self._error(status, body)
        try:
            return int(body["timestamp"])
        except (KeyError, TypeError, ValueError):
            return 0  # posted; replies and edits just won't link to it

    # ------------------------------------------------------------------ HTTP

    async def _get(self, path: str):
        status, body = await self._request("GET", path)
        if status != 200:
            raise self._error(status, body)
        return body

    async def _request(self, method: str, path: str, fields: Optional[dict] = None,
                       attachment: Optional[Tuple[Media, Path]] = None,
                       timeout: float = CHECK_TIMEOUT_S):
        """(status, parsed body). Retries once when nothing answered at all
        (container restarting) or the API had an internal error."""
        url = self.base_url + path
        body, headers = None, {}
        if fields is not None:
            body, length = await asyncio.to_thread(_build_body, fields, attachment)
            headers = {"Content-Type": "application/json", "Content-Length": str(length)}
        try:
            for attempt in (1, 2):
                if body is not None:
                    body.seek(0)
                try:
                    status, raw = await asyncio.to_thread(self._transport, method, url, body,
                                                          headers, timeout)
                except (OSError, http.client.HTTPException) as e:  # URLError is an OSError
                    if attempt == 1 and _never_delivered(e):
                        await asyncio.sleep(self.retry_delay)
                        continue
                    raise Unreachable(
                        f"signal-cli-rest-api at {self.base_url} isn't answering ({_reason(e)})", NAME,
                        hint="Check that the signal-api container is running, and that the bridge "
                             "was started after it",
                    ) from None
                if status >= 500 and attempt == 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                return status, _parse(raw)
        finally:
            if body is not None:
                body.close()

    def _error(self, status: int, body) -> Exception:
        message = str((body.get("error") if isinstance(body, dict) else body) or f"HTTP {status}").strip()
        low = message.lower()
        if status == 404:
            return Unreachable(f"{self.base_url} answered 404 ({message}); it doesn't look like "
                               f"signal-cli-rest-api", NAME, hint="Check SIGNAL_API_URL in .env")
        if status >= 500:
            return Unreachable(f"signal-cli-rest-api failed ({message})", NAME,
                               hint="Check its log: docker logs signal-api")
        if (isinstance(body, dict) and body.get("challenge_tokens")) or "rate limit" in low or "[429]" in low:
            return Unreachable(f"Signal is rate limiting the account ({message})", NAME,
                               hint="It may want a captcha; see signal-cli-rest-api's "
                                    "rate-limit-challenge endpoint")
        if any(s in low for s in _SETUP_ERRORS):
            return Unreachable(message, NAME,
                               hint="Check the destination id and the account in signal-cli-rest-api")
        return Rejected(message, NAME, retry_once=True)


# ---------------------------------------------------------------------- helpers

def as_plain(rich: fmt.Rich) -> fmt.Rich:
    """What Signal's plain text needs done to formatting before it's dropped."""
    return links_as_text(quotes_as_lines(rich))


def links_as_text(rich: fmt.Rich) -> fmt.Rich:
    """Plain text can't hide a link behind words, so show the URL after them,
    unless the words already contain it."""
    links = [s for s in rich.spans if s.kind == fmt.TEXT_URL and s.data]
    if not links:
        return rich
    others = fmt.Rich(rich.text, [s for s in rich.spans if s.kind != fmt.TEXT_URL])
    added = [(s.end, s.end, f" ({s.data})") for s in links if s.data not in rich.text[s.start:s.end]]
    return fmt.replace_ranges(others, added) if added else others


def quotes_as_lines(rich: fmt.Rich) -> fmt.Rich:
    """Signal gets plain text, so a quote block is shown the way people type
    one: each of its lines starts with "> ". Other spans are kept."""
    quotes = [s for s in rich.spans if s.kind == fmt.QUOTE]
    if not quotes:
        return rich
    starts = set()
    for s in quotes:
        starts.add(s.start)
        starts.update(i + 1 for i in range(s.start, s.end - 1) if rich.text[i] == "\n")
    others = fmt.Rich(rich.text, [s for s in rich.spans if s.kind != fmt.QUOTE])
    return fmt.replace_ranges(others, [(i, i, "> ") for i in sorted(starts)])


def _data_uri_prefix(media: Media, path: Path) -> str:
    mime = (media.mimetype or "").split(";")[0].strip().lower()
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    name = path.name.replace(";", "_").replace(",", "_")  # both delimit the data URI
    return f"data:{mime};filename={name};base64,"


def _build_body(fields: dict, attachment: Optional[Tuple[Media, Path]]) -> Tuple[BinaryIO, int]:
    """The /v2/send JSON, in a temporary file. An attachment (base64, possibly
    tens of MB) is streamed from its file rather than built in memory."""
    out = tempfile.TemporaryFile()
    if attachment is None:
        n = out.write(json.dumps(fields).encode())
    else:
        media, path = attachment
        head = (json.dumps(fields)[:-1] + ', "base64_attachments": ["'
                + json.dumps(_data_uri_prefix(media, path))[1:-1])
        n = out.write(head.encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(_B64_BLOCK), b""):
                n += out.write(base64.b64encode(block))
        n += out.write(b'"]}')
    out.seek(0)
    return out, n


def _parse(raw: bytes):
    text = (raw or b"").decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


def _never_delivered(e: Exception) -> bool:
    """True when the request surely didn't arrive (nothing listening, unknown
    host), so sending it again can't post the message twice."""
    reason = getattr(e, "reason", e)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _reason(e: Exception) -> str:
    return str(getattr(e, "reason", None) or e) or type(e).__name__
