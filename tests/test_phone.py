"""Open on your phone, from the chat page: the tailnet address, a sign-in link with a fresh code, and that link as a
QR code (crewchat_src/qr.py), so nobody has to type `crewchat url` and `crewchat ui --print` in a terminal."""
import json
import random
import string
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_crewchat import Base, crewchat  # noqa: E402  (the shared server fixture)

try:
    import numpy
    import zxingcpp
except ImportError:  # a real decoder, when installed: pip install zxing-cpp numpy
    zxingcpp = None


def scan(rows):
    """What a real QR reader makes of rows (qr_matrix's), drawn with the quiet zone and scaled up."""
    n, px = len(rows), 8
    img = numpy.full(((n + 8) * px, (n + 8) * px), 255, numpy.uint8)
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == "1":
                img[(r + 4) * px:(r + 5) * px, (c + 4) * px:(c + 5) * px] = 0
    found = zxingcpp.read_barcodes(img)
    return found[0].text if found else None


class QrCodes(unittest.TestCase):
    def test_sizes_and_finder_patterns(self):
        for text, size in (("hi", 21), ("x" * 40, 29), ("x" * 100, 37), ("x" * 134, 41)):
            rows = crewchat.qr_matrix(text)
            self.assertEqual((len(rows), {len(r) for r in rows}), (size, {size}), text)
            finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
            self.assertEqual([r[:7] for r in rows[:7]], finder)
            self.assertEqual([r[-7:] for r in rows[:7]], finder)
            self.assertEqual([r[:7] for r in rows[-7:]], finder)
            self.assertEqual(rows[6][8:-8], ("10" * size)[:size - 16])  # timing

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            crewchat.qr_matrix("x" * 135)

    @unittest.skipIf(zxingcpp is None, "a QR reader: pip install zxing-cpp numpy")
    def test_a_real_reader_reads_them(self):
        rnd = random.Random(5)
        texts = ["https://deepankars-macbook-pro.tail1a2b3c.ts.net/login?code=ABCDEFGH&chat=xv-test", "a", "é and ü"]
        texts += ["".join(rnd.choice(string.printable[:94]) for _ in range(rnd.randint(1, 134))) for _ in range(60)]
        for text in texts:
            self.assertEqual(scan(crewchat.qr_matrix(text)), text)


class PhonePanel(Base):
    def ask(self, op):
        req = urllib.request.Request(self.base + "/api/machines", data=json.dumps({"op": op}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.owner})
        with urllib.request.urlopen(req, timeout=20) as answer:
            return json.loads(answer.read().decode())

    def set_url(self, url):
        crewchat.change_config(lambda c: c.update(url=url))
        self.addCleanup(crewchat.change_config, lambda c: c.update(url=""))

    def test_a_sign_in_link_that_works_once_and_its_qr_code(self):
        self.set_url("https://mac.tail1234.ts.net")
        out = self.ask("phone")
        self.assertEqual(out["url"], "https://mac.tail1234.ts.net")
        self.assertTrue(out["link"].startswith("https://mac.tail1234.ts.net/login?code="))
        self.assertEqual(crewchat.qr_matrix(out["link"]), out["qr"])
        self.assertEqual(out["ttl"], crewchat.CODE_TTL)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(out["link"]).query)["code"][0]
        self.assertEqual(out["code"].replace("-", ""), code)
        status, _, headers = self.raw("/login?code=" + code)  # the phone opens the link: a session, and the chat
        self.assertEqual(status, 303)
        self.assertIn("crewchat_session=", headers.get("Set-Cookie", ""))
        _, _, again = self.raw("/login?code=" + code)
        self.assertNotIn("crewchat_session=", again.get("Set-Cookie") or "")  # single use

    def test_no_address_says_why_and_mints_no_code(self):
        with mock.patch.object(crewchat, "find_tailscale", return_value=None), \
                mock.patch.object(self.hub, "new_code") as minted:
            out = self.ask("phone")
        self.assertEqual(out["url"], "")
        self.assertIn("Tailscale is not installed", out["why"])
        self.assertFalse(out["can_setup"])  # no Set it up button: there is nothing to set up with
        minted.assert_not_called()

    def test_a_local_address_is_no_address_for_a_phone(self):
        self.set_url("http://127.0.0.1:8765")
        with mock.patch.object(crewchat, "find_tailscale", return_value=None):
            self.assertEqual(self.ask("phone")["url"], "")

    def test_set_it_up_runs_tailscale_serve_and_keeps_the_address(self):
        import crewchat_peers
        with mock.patch.object(crewchat, "find_tailscale", return_value="/usr/bin/tailscale"), \
                mock.patch.object(crewchat_peers, "tailscale_address",
                                  return_value=("https://mac.tail1234.ts.net", None)) as serve:
            out = self.ask("phone-setup")
        self.addCleanup(crewchat.change_config, lambda c: c.update(url=""))
        serve.assert_called_once()
        self.assertEqual(out["url"], "https://mac.tail1234.ts.net")
        self.assertEqual(crewchat.load_config()["url"], "https://mac.tail1234.ts.net")

    def test_set_it_up_says_what_tailscale_said(self):
        import crewchat_peers
        with mock.patch.object(crewchat, "find_tailscale", return_value="/usr/bin/tailscale"), \
                mock.patch.object(crewchat_peers, "tailscale_address",
                                  return_value=(None, "Tailscale is not signed in on this machine")):
            out = self.ask("phone-setup")
        self.assertEqual((out["url"], out["why"]), ("", "Tailscale is not signed in on this machine"))

    def test_only_the_owner(self):
        for token, code in ((self.token, 403), (None, 401)):
            req = urllib.request.Request(self.base + "/api/machines", data=b'{"op": "phone"}', method="POST",
                                         headers=dict({"Content-Type": "application/json"},
                                                      **({"Authorization": "Bearer " + token} if token else {})))
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=20)
            self.assertEqual(e.exception.code, code)

    def test_the_page_has_the_panel(self):
        text = crewchat.CHAT_PAGE
        self.assertIn('data-panel="phone"', text)
        self.assertIn("function panelPhone()", text)


if __name__ == "__main__":
    unittest.main()
