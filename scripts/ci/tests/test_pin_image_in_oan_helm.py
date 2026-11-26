"""The values-file edit of pin_image_in_oan_helm.py: one line changes, nothing else."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pin_image_in_oan_helm as pin  # noqa: E402

DIGEST = "sha256:" + "a" * 64
VALUES = """# New Dev values
cookieDomain: .development.oanstaging.com

ui:
  image:
    repository: 379220350808.dkr.ecr.ap-south-1.amazonaws.com/eoan/oan_messaging_ui/oan-messaging-ui
    tag: 1.0.0-develop.1   # pinned
  host: messaging.development.oanstaging.com

oan-messaging-service:
  image:
    tag: 1.0.0-develop.3
"""


class PinImage(unittest.TestCase):
    def test_each_image_moves_alone_and_keeps_every_other_line(self):
        for key, old, line in (("ui", "1.0.0-develop.1", 7), ("service", "1.0.0-develop.3", 12)):
            new = f"1.0.0-develop.9@{DIGEST}"
            out = pin.set_value(VALUES, pin.PATHS[key], new)
            self.assertEqual(pin.get_value(out, pin.PATHS[key]), new)
            changed = [
                i
                for i, (a, b) in enumerate(
                    zip(VALUES.splitlines(), out.splitlines(), strict=True), 1
                )
                if a != b
            ]
            self.assertEqual(changed, [line])
            self.assertEqual(pin.get_value(VALUES, pin.PATHS[key]), old)

    def test_keeps_trailing_comment_and_crlf(self):
        out = pin.set_value(
            VALUES.replace("\n", "\r\n"), pin.PATHS["ui"], f"1.0.0-develop.2@{DIGEST}"
        )
        self.assertIn(f"tag: 1.0.0-develop.2@{DIGEST}   # pinned\r\n", out)

    def test_missing_key_is_an_error(self):
        with self.assertRaises(ValueError):
            pin.set_value("ui:\n  host: x\n", pin.PATHS["ui"], "v")

    def test_only_a_digest_pinned_develop_tag_is_accepted(self):
        self.assertTrue(pin.TAG.fullmatch(f"1.0.0-develop.12@{DIGEST}"))
        for bad in (
            "1.0.0-develop.12",
            "latest@" + DIGEST,
            "1.0.0-pr-3.1@" + DIGEST,
            "1.0.0-develop.1@sha256:abc",
        ):
            self.assertFalse(pin.TAG.fullmatch(bad))


class Retry(unittest.TestCase):
    def serve(self, statuses):
        calls = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(1)
                code = statuses[min(len(calls), len(statuses)) - 1]
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps({"message": "boom"} if code >= 400 else {"ok": True}).encode())

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}/x", calls

    def test_a_transient_502_is_retried_and_a_404_is_not(self):
        pin.time.sleep = lambda s: None
        url, calls = self.serve([502, 502, 200])
        self.assertEqual(pin._send("GET", url, "t"), (200, {"ok": True}))
        self.assertEqual(len(calls), 3)
        url, calls = self.serve([404])
        self.assertEqual(pin._send("GET", url, "t"), (404, "boom"))
        self.assertEqual(len(calls), 1)
        url, calls = self.serve([502])
        self.assertEqual(pin._send("GET", url, "t")[0], 502)
        self.assertEqual(len(calls), 4)


class Promote(unittest.TestCase):
    """Staging form: New Dev's image moves only if staging holds the code it was built from."""

    DEV = VALUES.replace(
        "tag: 1.0.0-develop.1   # pinned",
        f"tag: 1.0.0-develop.5@{DIGEST}  # built from 0123456789ab",
    )
    HEAD = "f" * 40

    def run_promote(self, dev_text, trees):
        written = []

        def read(repo, token, branch, file):
            return {"sha": "x"}, dev_text

        def api(method, url, token, body=None):
            sha = url.rsplit("/", 1)[1]
            return {"tree": {"sha": trees[sha]}}

        def write(*args):
            written.append(args)
            return 0

        with (
            mock.patch.object(pin, "_read", read),
            mock.patch.object(pin, "api", api),
            mock.patch.object(pin, "_write", write),
            mock.patch.dict(pin.os.environ, {"SOURCE_REPO": "org/repo"}),
        ):
            code = pin.promote("ui", self.HEAD, "t", "https://api.github.com/repos/o/h", "main")
        return code, written

    def test_same_tree_pins_the_image_on_staging_with_its_source(self):
        code, written = self.run_promote(self.DEV, {"0123456789ab": "T1", self.HEAD: "T1"})
        self.assertEqual(code, 0)
        self.assertEqual(len(written), 1)
        args = written[0]
        self.assertEqual(args[3], pin.STAGING_VALUES)
        self.assertEqual(args[5], f"1.0.0-develop.5@{DIGEST}")
        self.assertEqual(args[6], "built from 0123456789ab")

    def test_a_different_tree_pins_nothing(self):
        code, written = self.run_promote(self.DEV, {"0123456789ab": "T1", self.HEAD: "T2"})
        self.assertEqual((code, written), (0, []))

    def test_a_pin_without_a_source_commit_pins_nothing(self):
        dev = self.DEV.replace("  # built from 0123456789ab", "")
        code, written = self.run_promote(dev, {})
        self.assertEqual((code, written), (0, []))

    def test_the_comment_replaces_the_old_one_only_when_given(self):
        out = pin.set_value(
            VALUES, pin.PATHS["ui"], f"1.0.0-develop.2@{DIGEST}", "built from abcdefabcdef"
        )
        self.assertIn(f"tag: 1.0.0-develop.2@{DIGEST}  # built from abcdefabcdef\n", out)
        self.assertEqual(pin.get_comment(out, pin.PATHS["ui"]), "built from abcdefabcdef")


if __name__ == "__main__":
    unittest.main()
