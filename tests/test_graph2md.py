"""Offline tests: a tiny fake Graph server, no Microsoft account needed.

Run:  python -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph2md import cli, graph as G  # noqa: E402

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def make_jwt(upn="someone@example.com", exp=4102444800, scp="Files.ReadWrite.All"):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    header = seg({"alg": "RS256", "typ": "JWT"})
    payload = seg({"upn": upn, "exp": exp, "scp": scp, "appid": "test-app"})
    return "%s.%s.%s" % (header, payload, "signature")


FILES = {
    "Notes/readme.md": ("# Notes\n\nhello\n", "text/markdown; charset=utf-8"),
    "Notes/plan.txt": ("line one\nline two\n", "text/plain"),
    "Vault/index.md": ("# Index\n", "text/markdown"),
    "Vault/Sub/deep.md": ("# Deep\n", "text/markdown"),
    "Vault/Sub/image.png": ("\x89PNG\r\n", "application/octet-stream"),
}

TREE = {
    "": [
        {"name": "Vault", "folder": {"childCount": 2}},
        {"name": "loose.md", "file": {}, "size": 5},
    ],
    "Vault": [
        {"name": "index.md", "file": {}, "size": 8, "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
        {"name": "Sub", "folder": {"childCount": 2}},
    ],
    "Vault/Sub": [
        {"name": "deep.md", "file": {}, "size": 7},
        {"name": "image.png", "file": {}, "size": 6},
    ],
}


class FakeGraph(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep test output clean
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype):
        body = data.encode() if isinstance(data, str) else data
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._json({"error": {"code": "InvalidAuthenticationToken",
                                         "message": "no bearer"}}, 401)
        # pagination: page 2 of the Vault listing
        if "page=2" in self.path:
            return self._json({"value": TREE["Vault"][1:]})
        raw = unquote(urlparse(self.path).path)
        if raw.endswith("/me/drive/root/children"):
            return self._json({"value": TREE[""]})
        marker = "/me/drive/root:"
        if marker not in raw:
            return self._json({"error": {"code": "itemNotFound", "message": raw}}, 404)
        rel = raw.split(marker, 1)[1]
        # ".../Vault:/children"  -> folder listing
        if rel.endswith(":/children"):
            folder = rel[:-len(":/children")].lstrip("/")
            items = TREE.get(folder)
            if items is None:
                return self._json({"error": {"code": "itemNotFound", "message": folder}}, 404)
            if folder == "Vault":  # serve the folder listing in two pages
                host, port = self.server.server_address
                return self._json({"value": items[:1],
                                   "@odata.nextLink": "http://%s:%d/v1.0/me/drive/root:/Vault:/children?page=2"
                                                       % (host, port)})
            return self._json({"value": items})
        if rel.endswith(":/content"):
            path = rel[:-len(":/content")].lstrip("/")
        else:
            path = rel.lstrip("/")
        hit = FILES.get(path)
        if hit is None:
            return self._json({"error": {"code": "itemNotFound", "message": path}}, 404)
        return self._bytes(hit[0], hit[1])


class FakeServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGraph)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1.0" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tokfile = os.path.join(self.tmp, "token.json")
        with open(self.tokfile, "w", encoding="utf-8") as fh:
            json.dump({"access_token": make_jwt(), "expires_on": "2099-01-01T00:00:00+00:00",
                       "scopes": ["Files.ReadWrite.All"]}, fh)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)


# --------------------------------------------------------------------------- #
# token tests
# --------------------------------------------------------------------------- #

class TokenTests(unittest.TestCase):
    def test_envelope_with_bom(self):
        tok = make_jwt()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("\ufeff" + json.dumps({"access_token": tok, "expires_on": "2099-01-01T00:00:00Z"}))
            path = fh.name
        try:
            loaded = G.load_token(path)
            self.assertEqual(loaded["access_token"], tok)
            self.assertEqual(loaded["expires_on"], "2099-01-01T00:00:00Z")
        finally:
            os.unlink(path)

    def test_raw_jwt_file_with_bom(self):
        tok = make_jwt()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("\ufeff" + tok + "\n")
            path = fh.name
        try:
            self.assertEqual(G.load_token(path)["access_token"], tok)
        finally:
            os.unlink(path)

    def test_bearer_prefix_and_quotes_are_stripped(self):
        tok = make_jwt()
        self.assertEqual(G.clean_token_text('  "Bearer %s"  ' % tok), tok)

    def test_token_as_literal_argument(self):
        tok = make_jwt()
        self.assertEqual(G.load_token(tok)["access_token"], tok)

    def test_env_var(self):
        tok = make_jwt()
        old = os.environ.get("GRAPH_TOKEN")
        os.environ["GRAPH_TOKEN"] = tok
        try:
            self.assertEqual(G.load_token()["access_token"], tok)
        finally:
            if old is None:
                del os.environ["GRAPH_TOKEN"]
            else:
                os.environ["GRAPH_TOKEN"] = old

    def test_missing_token_raises(self):
        old = os.environ.pop("GRAPH_TOKEN", None)
        oldf = os.environ.pop("GRAPH_TOKEN_FILE", None)
        try:
            with self.assertRaises(G.GraphError):
                G.load_token()
        finally:
            if old is not None:
                os.environ["GRAPH_TOKEN"] = old
            if oldf is not None:
                os.environ["GRAPH_TOKEN_FILE"] = oldf

    def test_status_reports_identity_and_expiry(self):
        st = G.token_status({"access_token": make_jwt(upn="a@b.c", exp=4102444800)})
        self.assertEqual(st["upn"], "a@b.c")
        self.assertIn("Files.ReadWrite.All", st["scopes"])
        self.assertIn("2100-01-01", st["expires_on"])
        self.assertGreater(st["hours_left"], 0)


# --------------------------------------------------------------------------- #
# path + extension helpers
# --------------------------------------------------------------------------- #

class HelperTests(unittest.TestCase):
    def test_ext_from_content_type(self):
        self.assertEqual(G.ext_for("text/markdown; charset=utf-8"), ".md")
        self.assertEqual(G.ext_for("text/plain"), ".txt")
        self.assertEqual(G.ext_for("application/octet-stream"), ".bin")

    def test_ext_falls_back_to_name(self):
        self.assertEqual(G.ext_for("", "a/b/note.md"), ".md")
        self.assertEqual(G.ext_for("application/octet-stream", "x/y.pptx"), ".pptx")

    def test_safe_relative_path(self):
        self.assertEqual(G.safe_relative_path("a/b.md"), os.path.join("a", "b.md"))
        for bad in ("../evil.md", "/etc/passwd", "a/../../b.md", ""):
            with self.assertRaises(ValueError):
                G.safe_relative_path(bad)


# --------------------------------------------------------------------------- #
# end-to-end against the fake server
# --------------------------------------------------------------------------- #

class CliTests(FakeServerMixin, unittest.TestCase):
    def run_cli(self, *args):
        return cli.main(list(args) + ["--token-file", self.tokfile, "--base", self.base, "--quiet"])

    def test_get_single_file(self):
        rc = self.run_cli("get", "Notes/readme.md", "-o", "out.md")
        self.assertEqual(rc, 0)
        self.assertEqual(open("out.md", encoding="utf-8").read(), "# Notes\n\nhello\n")

    def test_get_strips_bom_on_write(self):
        import shutil
        src = os.path.join(self.tmp, "bom.md")
        self.run_cli("get", "Vault/index.md", "-o", src)
        self.assertTrue(os.path.exists(src))

    def test_get_refuses_overwrite_without_force(self):
        self.run_cli("get", "Notes/readme.md", "-o", "out.md")
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["get", "Notes/readme.md", "-o", "out.md",
                      "--token-file", self.tokfile, "--base", self.base])
        self.assertEqual(ctx.exception.code, 2)

    def test_get_force_overwrites(self):
        self.run_cli("get", "Notes/readme.md", "-o", "out.md")
        self.assertEqual(self.run_cli("get", "Notes/plan.txt", "-o", "out.md", "--force"), 0)
        self.assertIn("line one", open("out.md", encoding="utf-8").read())

    def test_missing_file_is_a_clean_error(self):
        rc = self.run_cli("get", "Notes/nope.md", "-o", "x.md")
        self.assertEqual(rc, 1)

    def test_tree_recurses_and_filters(self):
        rc = self.run_cli("tree", "Vault", "--out", "copy")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join("copy", "index.md")))
        self.assertTrue(os.path.exists(os.path.join("copy", "Sub", "deep.md")))
        self.assertFalse(os.path.exists(os.path.join("copy", "Sub", "image.png")))

    def test_tree_handles_pagination(self):
        g = G.Graph({"access_token": make_jwt()}, base=self.base)
        names = [i["name"] for i in g.children("Vault")]
        self.assertEqual(sorted(names), ["Sub", "index.md"])

    def test_tree_dry_run_writes_nothing(self):
        rc = self.run_cli("tree", "Vault", "--out", "copy", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists("copy"))

    def test_list_filters_by_extension(self):
        g = G.Graph({"access_token": make_jwt()}, base=self.base)
        items = g.children("Vault/Sub")
        self.assertEqual(len(items), 2)

    def test_status_ok(self):
        self.assertEqual(self.run_cli("status"), 0)

    def test_401_is_reported_with_hint(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            json.dump({"access_token": "a.b.c"}, fh)
        rc = cli.main(["status", "--token-file", bad, "--base", self.base, "--quiet"])
        self.assertIn(rc, (0, 3))


class ErrorTests(unittest.TestCase):
    def test_error_hint_is_attached(self):
        err = G.GraphError(401, "InvalidAuthenticationToken", "Access token has expired.")
        self.assertIn("InvalidAuthenticationToken", str(err))
        self.assertIn("fresh one", str(err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
