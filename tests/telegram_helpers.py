"""A fake Telethon bot client, for the Telegram sink and the bridge tests."""


class Sent:
    def __init__(self, id_):
        self.id = id_


class FakeBot:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.fail = []  # exceptions to raise on the next sends or edits, in order

    def _sent(self):
        self.next_id += 1
        return Sent(self.next_id)

    def _maybe_fail(self):
        if self.fail:
            raise self.fail.pop(0)

    async def send_message(self, peer, text, **kw):
        self._maybe_fail()
        self.calls.append(("message", peer, text, kw))
        return self._sent()

    async def send_file(self, peer, file, **kw):
        self._maybe_fail()
        self.calls.append(("file", peer, file, kw))
        return self._sent()

    async def edit_message(self, peer, msg_id, text, **kw):
        self._maybe_fail()
        self.calls.append(("edit", peer, msg_id, text, kw))

    def is_connected(self):
        return False
