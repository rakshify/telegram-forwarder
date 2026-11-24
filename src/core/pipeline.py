"""The pipeline: a source's queued messages -> every pair's destination.

What happens to each message
----------------------------
1. The source writes each message from a configured chat to the on-disk
   queue (store.inbox) as soon as it arrives (Host.enqueue).
2. A single worker takes queued messages in arrival order, has the source
   decode each into a neutral Message, downloads its media once, and posts
   it to every pair's destination through that platform's sink.
3. The destination's message id is saved per pair (store.msgmap), so
   replies thread, edits apply, and a retry never posts the same message
   twice.

One destination's trouble doesn't hold up the others: a destination that
can't be reached is paused and checked again every minute, and a failed
send is retried after every other destination has had the message.

Platform specifics stay in the source (e.g. src/adapters/whatsapp/source.py)
and the sinks (src/adapters/*/sink.py); nothing here imports a platform
library.
"""
import asyncio
import dataclasses
import signal
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import rich as fmt
from .model import Message, Pair, Quote, Skip
from .sink import CantEdit, Notifier, RateLimited, Rejected, Sink, Unreachable
from .source import MediaUnavailable, NotConnected, Source
from .store import InboxItem, Store

RETRY_DELAYS = (5, 15, 45, 120, 300)  # seconds; then the message is given up
ALERT_REPEAT_S = 3600
RESOLVE_EVERY_S = 60     # how often unreachable destinations are checked again
WATCHDOG_EVERY_S = 30
PRUNE_EVERY_S = 86400
SHUTDOWN_GRACE_S = 6     # lets an in-flight send finish; Docker allows ~10 s before SIGKILL


class Pipeline:
    def __init__(self, *, source: Source, pairs: List[Pair], sinks: Dict[str, Sink], store: Store,
                 title: str, alerts: Optional[int] = None, notifier: Optional[Notifier] = None,
                 dry_run: bool = False):
        self.source = source
        self.pairs = pairs
        self.sinks = sinks        # platform -> sink
        self.store = store
        self.title = title        # names this process in alerts
        self.alerts = alerts      # who gets them: a Telegram user id
        self.notifier = notifier  # what sends them: the Telegram sink
        self.dry_run = dry_run
        self.routes: Dict[str, List[Pair]] = {}
        for pair in pairs:
            self.routes.setdefault(pair.source, []).append(pair)
        self.sinks_started = False
        self.wakeup = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.exit_code = 0
        self._alerted: Dict[str, float] = {}
        source.attach(self)

    # ================================================================ lifecycle

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

        await self._start_sinks()
        pending = self.store.pending_count()
        if pending:
            print(f"  {pending} message(s) still queued from the last run; sending them first.")
        tasks = [
            asyncio.create_task(self._worker(), name="worker"),
            asyncio.create_task(self._watchdog(), name="watchdog"),
        ]
        await self.source.start()

        await self.stop_event.wait()
        print("Stopping...")
        await self._shutdown(tasks)
        return self.exit_code

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self, exit_code: Optional[int] = None) -> None:
        if exit_code is not None:
            self.exit_code = exit_code
        self.stop_event.set()
        self.wakeup.set()

    async def sleep(self, seconds: float) -> None:
        """Sleep, but wake up early when stopping."""
        try:
            await asyncio.wait_for(self.stop_event.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _start_sinks(self) -> None:
        for sink in self.sinks.values():
            await sink.start()
        self.sinks_started = True
        for pair in self.pairs:
            await self._resolve_dest(pair, first_try=True)

    async def _shutdown(self, tasks: List[asyncio.Task]) -> None:
        worker = tasks[0]
        try:
            await asyncio.wait_for(asyncio.shield(worker), SHUTDOWN_GRACE_S)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.source.stop()
        for sink in self.sinks.values():
            await sink.stop()
        self.store.close()

    # ================================================================ for the source

    def enqueue(self, chat: str, msg_id: str, payload: bytes, digest: str = "") -> bool:
        if not self.store.enqueue(chat, msg_id, payload, digest):
            return False
        self.wakeup.set()
        return True

    def wake(self) -> None:
        self.wakeup.set()

    def dests_label(self, chat: str) -> str:
        return ", ".join(
            f"{p.dest.label}"
            f"{'' if p.dest.handle is not None or self.dry_run else ' (not reachable yet)'}"
            for p in self.routes.get(chat, [])
        )

    async def alert(self, key: str, text: str) -> None:
        print(f"ALERT: {text}")
        if self.dry_run or not self.alerts or self.notifier is None or not self.notifier.ready:
            return
        now = time.time()
        if now - self._alerted.get(key, 0) < ALERT_REPEAT_S:
            return
        self._alerted[key] = now
        try:
            await self.notifier.notify(self.alerts, f"⚠️ {self.title}: {text}")
        except Exception as e:
            print(f"  (couldn't send the alert to {self.alerts}: {e}. "
                  f"That user has to press Start on the bot once.)")

    # ================================================================ destinations

    async def _resolve_dest(self, pair: Pair, first_try: bool = False) -> bool:
        """Check that the pair's destination can be posted to.

        One that can't (bot not added yet, wrong id, Signal API down) only
        pauses that pair: the others keep working, and the watchdog checks
        again every minute, so fixing the cause is enough.
        """
        dest = pair.dest
        try:
            dest.handle = await self.sinks[dest.platform].resolve(dest)
        except Unreachable as e:
            dest.handle = None
            if first_try:
                print(f"  WARNING: {e}")
                print(f"  Messages for {dest.label} are skipped until it's reachable; "
                      f"checking again every minute.")
                await self.alert(f"dest:{pair.key}", self._unreachable_text(pair, e))
            return False
        if not first_try:
            print(f"{dest.title} is reachable now; forwarding {pair.source} to it.")
        return True

    def _unreachable_text(self, pair: Pair, e: Unreachable) -> str:
        hint = f" {e.hint};" if e.hint else ""
        return (f"can't post to {pair.dest.title} (source {pair.source}): {e}.{hint} "
                f"forwarding starts within a minute of fixing it.")

    async def _pause(self, pair: Pair, e: Unreachable) -> None:
        """A destination stopped answering mid-run: skip it until the watchdog
        finds it reachable again, rather than holding up the queue."""
        pair.dest.handle = None
        print(f"[{self.source.label(pair.source)} -> {pair.dest.label}] {e}; skipping it until "
              f"it's reachable again (checked every minute)")
        await self.alert(f"dest:{pair.key}", self._unreachable_text(pair, e))

    # ================================================================ worker

    async def _worker(self) -> None:
        while not self.stopping:
            # Clear before checking: an enqueue after this point sets it again.
            self.wakeup.clear()
            item = self.store.next_pending()
            if item is None:
                await self.wakeup.wait()
                continue
            await self._handle(item)

    async def _watchdog(self) -> None:
        last_prune = 0.0
        last_resolve = time.time()
        while not self.stopping:
            now = time.time()
            await self.source.check()
            if now - last_prune > PRUNE_EVERY_S and not self.dry_run:
                self.store.prune()
                last_prune = now
            if now - last_resolve >= RESOLVE_EVERY_S and self.sinks_started:
                last_resolve = now
                for pair in self.pairs:
                    if pair.dest.handle is None:
                        await self._resolve_dest(pair)
            await self.sleep(WATCHDOG_EVERY_S)

    async def _handle(self, item: InboxItem) -> None:
        label = self.source.label(item.source)
        try:
            msg = self.source.decode(item)
        except Exception as e:
            # The same payload would fail the same way again: give it up now.
            self.store.mark_failed(item.seq, f"unreadable queue entry: {e!r}")
            print(f"[{label}] message {item.msg_id} can't be read: {e!r}")
            await self.alert(f"unreadable:{item.source}", f"a message from {label} couldn't be "
                             f"read, so it was skipped: {e!r}")
            return
        try:
            await self._process(item.source, item.msg_id, msg)
        except NotConnected:
            # Not a failure: wait until the source is back, then try again.
            while not self.source.connected and not self.stopping:
                await self.sleep(5)
            return
        except RateLimited as e:
            print(f"[{label}] {e.platform} rate limit: waiting {e.seconds}s")
            await self.sleep(e.seconds + 1)
            return
        except Rejected as e:
            # The platform refused it (bot removed, no rights, bad input).
            # Some refusals get one more try, in case it was an upload glitch.
            attempts = self.store.record_attempt(item.seq, repr(e))
            if e.retry_once and attempts < 2:
                print(f"[{label}] {e.platform} rejected message {item.msg_id} ({e}); trying once more")
                await self.sleep(RETRY_DELAYS[0])
                return
            self.store.mark_failed(item.seq, repr(e))
            print(f"[{label}] message {item.msg_id} not forwarded: {e}")
            await self.alert(f"rejected:{item.source}", f"{e.platform} rejected a message from "
                             f"{label}: {e}")
            return
        except Exception as e:
            attempts = self.store.record_attempt(item.seq, repr(e))
            if attempts > len(RETRY_DELAYS):
                self.store.mark_failed(item.seq, repr(e))
                print(f"[{label}] giving up on message {item.msg_id} after {attempts} attempts: {e!r}")
                await self.alert(f"failed:{item.source}", f"gave up forwarding a message from "
                                 f"{label} after {attempts} attempts: {e!r}")
            else:
                delay = RETRY_DELAYS[attempts - 1]
                print(f"[{label}] error on message {item.msg_id}: {e!r}; "
                      f"retry {attempts}/{len(RETRY_DELAYS)} in {delay}s")
                await self.sleep(delay)
            return
        self.store.mark_done(item.seq)

    async def _process(self, chat: str, msg_id: str, msg: Union[Message, Skip]) -> None:
        pairs = self.routes.get(chat, [])
        label = self.source.label(chat)
        if isinstance(msg, Skip):
            if not msg.quiet:
                print(f"[{label}] skipped {msg_id}: {msg.reason}")
            return

        failures: List[Exception] = []
        if msg.kind == "edit":
            for pair in pairs:
                if pair.dest.handle is not None or self.dry_run:
                    await self._isolated(pair, self._apply_edit(pair, msg), failures)
            if failures:
                raise failures[0]
            return

        for p in pairs:
            if p.dest.handle is None and not self.dry_run:
                print(f"[{label}] {msg.id} not forwarded to {p.dest.label}: it isn't reachable yet")
        todo = [p for p in pairs
                if (p.dest.handle is not None or self.dry_run)
                and self.store.posted(p.key, msg.id) is None]
        if not todo:
            return
        if self.dry_run:
            for pair in todo:
                self._print_dry_run(pair, msg)
            return

        with tempfile.TemporaryDirectory(prefix="media-") as tmp:
            path = None
            if msg.media is not None:
                try:
                    path = await self.source.download(msg, Path(tmp))
                except MediaUnavailable as e:
                    print(f"[{label}] media of {msg.id} unavailable ({e}); posting the text only")
                    note = f"⚠️ The attached media couldn't be downloaded from {self.source.name}."
                    msg.body = fmt.concat(msg.body, fmt.plain("\n\n" if msg.body.text else ""),
                                          fmt.styled(note, fmt.ITALIC))
                    msg.kind, msg.media = "text", None
            for pair in todo:
                sent_id = await self._isolated(pair, self._send(pair, msg, path), failures)
                if sent_id is None:
                    continue
                self.store.record_posted(pair.key, msg.id, sent_id, is_media=msg.kind == "media")
                print(f"[{label} -> {pair.dest.label}] {msg.label} {msg.id} -> {sent_id}")
        if failures:
            raise failures[0]  # retried later; the pairs done above are skipped then

    async def _isolated(self, pair: Pair, attempt, failures: List[Exception]):
        """Await one destination's send so that its failure can't stop the
        others: an unreachable one is paused, any other error is collected for
        the caller to raise once every destination has had its turn."""
        try:
            return await attempt
        except Unreachable as e:
            await self._pause(pair, e)
        except Exception as e:
            failures.append(e)
        return None

    # ================================================================ composing

    @staticmethod
    def _quote_block(quote: Quote) -> fmt.Rich:
        inner = fmt.concat(fmt.styled(quote.sender_name or "someone", fmt.BOLD), fmt.plain(": "),
                           quote.preview)
        block = fmt.Rich(inner.text, inner.spans + [fmt.Span(fmt.QUOTE, 0, len(inner.text))])
        return fmt.concat(block, fmt.plain("\n"))

    def _reply_target(self, pair: Pair, msg: Message) -> Tuple[Optional[int], Optional[fmt.Rich]]:
        """(the destination's message to reply to, quote block to prepend instead)."""
        reply_to, quote = None, None
        if msg.reply is not None:
            hit = self.store.posted(pair.key, msg.reply.msg_id)
            if hit is not None:
                reply_to = hit[0]
            elif msg.reply.preview.text:
                quote = self._quote_block(msg.reply)
        return reply_to, quote

    @staticmethod
    def _compose(pair: Pair, msg: Message, body: fmt.Rich, quote: Optional[fmt.Rich]) -> fmt.Rich:
        parts = []
        if pair.attribution:
            parts.append(fmt.styled(msg.sender_name or "Unknown", fmt.BOLD))
            parts.append(fmt.plain(":\n\n" if (quote or body.text) else ":"))
        if quote is not None:
            parts.append(quote)
        parts.append(body)
        return fmt.strip(fmt.concat(*parts))

    def _print_dry_run(self, pair: Pair, msg: Message) -> None:
        reply_to, quote = self._reply_target(pair, msg)
        if msg.kind == "poll":
            shown = f"{msg.poll.question} {msg.poll.options}"
        else:
            shown = self._compose(pair, msg, msg.body, quote).text
        shown = shown.replace("\n", " / ")
        extra = f", {msg.media.kind} {msg.media.mimetype}" if msg.media else ""
        threaded = f", reply to {reply_to}" if reply_to else ""
        print(f"[dry run {self.source.label(pair.source)} -> {pair.dest.label}] "
              f"{msg.label}{extra}{threaded}: {shown[:160]!r}")

    # ================================================================ sending

    async def _send(self, pair: Pair, msg: Message, path: Optional[Path]) -> Optional[int]:
        sink = self.sinks[pair.dest.platform]
        reply_to, quote = self._reply_target(pair, msg)
        if msg.kind == "poll":
            poll = msg.poll
            question = poll.question.strip()
            if pair.attribution:
                question = f"{msg.sender_name or 'Unknown'}: {question}"
            sent = await sink.send_poll(pair.dest, dataclasses.replace(poll, question=question), reply_to)
            if sent is not None:
                return sent
            # The platform can't hold this poll; post it as a numbered list instead.
            lines = [f"📊 {poll.question}"] + [f"{i}. {o}" for i, o in enumerate(poll.options, 1)]
            rich = self._compose(pair, msg, fmt.plain("\n".join(lines)), None)
            return await sink.send_text(pair.dest, rich, reply_to)
        rich = self._compose(pair, msg, msg.body, quote)
        if msg.kind == "media":
            return await sink.send_media(pair.dest, msg.media, rich, path, reply_to)
        return await sink.send_text(pair.dest, rich, reply_to)

    async def _apply_edit(self, pair: Pair, msg: Message) -> None:
        hit = self.store.posted(pair.key, msg.edit_of)
        if hit is None:
            return  # the original was never forwarded
        sent_id, is_media = hit
        rich = self._compose(pair, msg, msg.body, None)
        if not rich.text and not is_media:
            return
        label = self.source.label(pair.source)
        if self.dry_run:
            print(f"[dry run {label} -> {pair.dest.label}] edit {sent_id}: {rich.text[:160]!r}")
            return
        try:
            changed = await self.sinks[pair.dest.platform].edit(pair.dest, sent_id, rich, is_media)
        except CantEdit as e:
            print(f"[{label} -> {pair.dest.label}] couldn't apply edit to {sent_id}: {e}")
            return
        if changed:
            print(f"[{label} -> {pair.dest.label}] edit {msg.edit_of} -> {sent_id}")
