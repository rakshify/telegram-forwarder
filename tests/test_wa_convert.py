import unittest

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage

from src.adapters.whatsapp import convert
from tests import wa_helpers as h

NAMES = {"2222@lid": "Priya", "919800000000@s.whatsapp.net": "Amit"}


def names(jid):
    return NAMES.get(jid)


def plan(msg, **kw):
    return convert.plan_message(h.event(msg, **kw), names)


class TextTests(unittest.TestCase):
    def test_plain_text(self):
        p = plan(h.text("  hello  "))
        self.assertEqual((p.kind, p.body.text, p.reply), ("text", "hello", None))

    def test_formatting_and_mentions(self):
        p = plan(h.ext_text("*hi* @2222 and @919800000000 and @5555",
                            mentions=["2222@lid", "919800000000@s.whatsapp.net", "5555@lid"]))
        self.assertEqual(p.body.text, "hi @Priya and @Amit and @someone")
        self.assertEqual([(s.kind, s.start, s.end) for s in p.body.spans], [("bold", 0, 2)])

    def test_reply_carries_quote(self):
        p = plan(h.ext_text("agreed", reply_to="WA0", quoted_sender="2222@lid",
                            quoted=h.text("buy *above* 120")))
        self.assertEqual(p.reply.msg_id, "WA0")
        self.assertEqual(p.reply.sender, "2222@lid")
        self.assertEqual(p.reply.preview.text, "buy above 120")

    def test_reply_to_photo_previews_label(self):
        p = plan(h.ext_text("nice", reply_to="WA0", quoted=h.image()))
        self.assertEqual(p.reply.preview.text, "📷 Photo")


class MediaTests(unittest.TestCase):
    def test_photo(self):
        p = plan(h.image("chart _today_"))
        self.assertEqual((p.kind, p.media.kind, p.media.field), ("media", "photo", "imageMessage"))
        self.assertEqual((p.media.width, p.media.height, p.body.text), (800, 600, "chart today"))

    def test_video_and_gif(self):
        self.assertEqual(plan(h.video("v")).media.kind, "video")
        gif = plan(h.video(gif=True)).media
        self.assertEqual((gif.kind, gif.field, gif.seconds), ("animation", "videoMessage", 12))

    def test_round_video_is_a_video(self):
        m = WAMessage()
        m.ptvMessage.mimetype = "video/mp4"
        m.ptvMessage.seconds = 5
        p = plan(m)
        self.assertEqual((p.media.kind, p.media.field), ("video", "videoMessage"))

    def test_voice_and_audio(self):
        v = plan(h.voice()).media
        self.assertEqual((v.kind, v.seconds, len(v.waveform)), ("voice", 7, 64))
        self.assertEqual(plan(h.voice(ptt=False)).media.kind, "audio")

    def test_document(self):
        p = plan(h.document("Q3 results.pdf", caption="see p.4"))
        self.assertEqual((p.media.kind, p.media.filename, p.body.text),
                         ("document", "Q3 results.pdf", "see p.4"))

    def test_document_with_caption_envelope(self):
        p = plan(h.wrap("documentWithCaptionMessage", h.document("a.pdf", "cap")))
        self.assertEqual((p.media.kind, p.body.text), ("document", "cap"))

    def test_other_envelopes(self):
        self.assertEqual(plan(h.wrap("ephemeralMessage", h.text("bye"))).body.text, "bye")
        self.assertEqual(plan(h.wrap("associatedChildMessage", h.image("in album"))).media.kind, "photo")

    def test_stickers(self):
        p = plan(h.sticker(animated=True))
        self.assertEqual((p.media.kind, p.media.animated, p.label), ("sticker", True, "sticker"))
        self.assertIsInstance(plan(h.sticker(lottie=True)), convert.Skip)

    def test_spoiler(self):
        p = plan(h.wrap("spoilerMessage", h.text("the ending")))
        self.assertIn(("spoiler", 0, 10), [(s.kind, s.start, s.end) for s in p.body.spans])


class OtherTypesTests(unittest.TestCase):
    def test_poll(self):
        p = plan(h.poll("Which stock?", ["TCS", "INFY", "HDFC"], selectable=0))
        self.assertEqual((p.kind, p.poll.question, p.poll.options, p.poll.multiple),
                         ("poll", "Which stock?", ["TCS", "INFY", "HDFC"], True))
        single = plan(h.poll("Q", ["a", "b"], selectable=1, field="pollCreationMessage")).poll
        self.assertFalse(single.multiple)

    def test_location(self):
        m = WAMessage()
        m.locationMessage.degreesLatitude = 19.076
        m.locationMessage.degreesLongitude = 72.8777
        m.locationMessage.name = "Office"
        p = plan(m)
        self.assertEqual(p.body.text, "📍 Office\nhttps://maps.google.com/?q=19.076000,72.877700")

    def test_contact(self):
        m = WAMessage()
        m.contactMessage.displayName = "Broker"
        m.contactMessage.vcard = "BEGIN:VCARD\nFN:Broker\nTEL;type=CELL;waid=91999:+91 99999 00000\nEND:VCARD"
        self.assertEqual(plan(m).body.text, "👤 Broker\n+91 99999 00000")

    def test_event(self):
        m = WAMessage()
        m.eventMessage.name = "Earnings call"
        m.eventMessage.startTime = 1700000000
        self.assertEqual(plan(m).body.text, "📅 Earnings call\n🕒 2023-11-14 22:13 UTC")

    def test_group_invite_has_no_link(self):
        m = WAMessage()
        m.groupInviteMessage.groupName = "Tips"
        m.groupInviteMessage.inviteCode = "SECRET"
        text = plan(m).body.text
        self.assertIn("Tips", text)
        self.assertNotIn("SECRET", text)

    def test_buttons_message_text(self):
        m = WAMessage()
        m.buttonsMessage.contentText = "Pick one"
        m.buttonsMessage.footerText = "bot"
        self.assertEqual(plan(m).body.text, "Pick one\nbot")


class SkipAndEditTests(unittest.TestCase):
    def test_edit(self):
        p = plan(h.edit("WA7", "*fixed* text"))
        self.assertEqual((p.kind, p.edit_of, p.body.text), ("edit", "WA7", "fixed text"))

    def test_deleted(self):
        m = WAMessage()
        m.protocolMessage.type = 0
        m.protocolMessage.key.ID = "WA7"
        s = plan(m)
        self.assertIsInstance(s, convert.Skip)
        self.assertFalse(s.quiet)

    def test_view_once(self):
        self.assertIsInstance(plan(h.wrap("viewOnceMessageV2", h.image())), convert.Skip)
        self.assertIsInstance(plan(h.image(), IsViewOnce=True), convert.Skip)

    def test_bookkeeping_is_quiet(self):
        m = WAMessage()
        m.reactionMessage.text = "👍"
        self.assertTrue(plan(m).quiet)
        m = WAMessage()
        m.senderKeyDistributionMessage.groupID = "x"
        self.assertTrue(plan(m).quiet)
        self.assertTrue(plan(WAMessage()).quiet)

    def test_sender_key_alongside_text_is_ignored(self):
        m = h.text("real")
        m.senderKeyDistributionMessage.groupID = "x"
        self.assertEqual(plan(m).body.text, "real")

    def test_bookkeeping_detection(self):
        def ev(msg):
            return h.event(msg)
        handover = WAMessage()
        handover.senderKeyDistributionMessage.groupID = "g"
        handover.messageContextInfo.deviceListMetadataVersion = 2
        self.assertTrue(convert.is_bookkeeping(ev(handover)))
        self.assertTrue(convert.is_bookkeeping(ev(WAMessage())))
        history = WAMessage()
        history.protocolMessage.type = 5  # HISTORY_SYNC_NOTIFICATION
        self.assertTrue(convert.is_bookkeeping(ev(history)))
        revoke = WAMessage()
        revoke.protocolMessage.type = 0
        self.assertFalse(convert.is_bookkeeping(ev(revoke)))
        self.assertFalse(convert.is_bookkeeping(ev(h.edit("A", "x"))))
        with_text = h.text("hi")
        with_text.senderKeyDistributionMessage.groupID = "g"
        self.assertFalse(convert.is_bookkeeping(ev(with_text)))
        self.assertFalse(convert.is_bookkeeping(ev(h.wrap("viewOnceMessage", h.image()))))

    def test_unsupported(self):
        m = WAMessage()
        m.requestPaymentMessage.currencyCodeIso4217 = "INR"
        s = plan(m)
        self.assertIsInstance(s, convert.Skip)
        self.assertIn("requestPaymentMessage", s.reason)


if __name__ == "__main__":
    unittest.main()
