"""Microsoft Graph plumbing: token loading, HTTP client, file walking.

Standard library only — no requests, no msal. You bring an access token; this
module spends it.
"""
from __future__ import annotations

import base64
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://graph.microsoft.com/v1.0"
TEXT_EXTS = {".md", ".markdown", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".log"}

CTYPE_EXT = {
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
    "application/pdf": ".pdf",
}

ERROR_TOKEN_HINTS = {
    "InvalidAuthenticationToken": "token expired or malformed — get a fresh one",
    "InvalidAudience": "token was minted for a different resource",
    "AccessDenied": "token lacks the scope for this path (Files.ReadWrite.All / Files.Read.All)",
    "itemNotFound": "no item at that path — check spelling, case, and the folder chain",
}


class GraphError(RuntimeError):
    """A Graph call failed. Carries status + parsed error body."""

    def __init__(self, status, code, message, url=""):
        self.status = status
        self.code = code
        self.message = message
        self.url = url
        hint = ERROR_TOKEN_HINTS.get(code)
        detail = "%s %s" % (status, code)
        if message:
            detail += ": " + message
        if hint:
            detail += " [hint: %s]" % hint
        super().__init__(detail)


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #

def decode_jwt_claims(token):
    """Decode a JWT payload without verifying it. Returns {} if it isn't one."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload)
        return json.loads(raw)
    except Exception:
        return {}


def clean_token_text(text):
    """Strip BOMs, whitespace and the usual paste debris from a token string."""
    text = text.replace("\ufeff", "").strip()
    for junk in ('"', "'"):
        if text.startswith(junk) and text.endswith(junk):
            text = text[1:-1]
    if text.lower().startswith("bearer "):
        text = text[7:]
    return text.strip()


def load_token(path=None, env_var="GRAPH_TOKEN"):
    """Return an access token.

    Priority: explicit path/string -> environment -> nothing.

    `path` may be a file holding either a JSON envelope
    ({"access_token": ..., "expires_on": ...}) or a raw JWT, and it may have a
    UTF-8 BOM in front (OneDrive/Windows editors love adding one).
    """
    if path:
        if os.path.exists(os.path.expanduser(path)):
            with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            envelope = _as_envelope(text)
            if envelope is not None:
                return envelope
            return {"access_token": clean_token_text(text), "expires_on": None}
        # not a file: treat it as the token itself
        return {"access_token": clean_token_text(path), "expires_on": None}

    env_path = os.environ.get(env_var + "_FILE") or os.environ.get("GRAPH_TOKEN_FILE")
    if env_path:
        return load_token(env_path)

    raw = os.environ.get(env_var) or os.environ.get("GRAPH_TOKEN")
    if raw:
        env = _as_envelope(raw)
        if env is not None:
            return env
        return {"access_token": clean_token_text(raw), "expires_on": None}

    raise GraphError(401, "NoToken", "no token: pass --token-file, or set GRAPH_TOKEN/GRAPH_TOKEN_FILE")


def _as_envelope(text):
    """Parse a JSON envelope if the text is one, else None."""
    stripped = text.replace("\ufeff", "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        doc = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    tok = doc.get("access_token") or doc.get("token")
    if not tok:
        return None
    return {
        "access_token": clean_token_text(str(tok)),
        "expires_on": doc.get("expires_on") or doc.get("expires_at"),
        "scopes": doc.get("scopes"),
    }


def token_status(token):
    """Human summary of a token: who, expiry, scopes. Never returns the token."""
    claims = decode_jwt_claims(token["access_token"])
    exp = token.get("expires_on")
    if not exp and claims.get("exp"):
        import datetime as _dt
        exp = _dt.datetime.utcfromtimestamp(int(claims["exp"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    left = None
    if exp:
        import datetime as _dt
        try:
            when = _dt.datetime.strptime(exp.replace("+00:00", "Z").replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
            left = (when - _dt.datetime.utcnow()).total_seconds() / 3600.0
        except Exception:
            left = None
    return {
        "upn": claims.get("upn") or claims.get("unique_name"),
        "appid": claims.get("appid"),
        "scopes": claims.get("scp") or token.get("scopes"),
        "expires_on": exp,
        "hours_left": None if left is None else round(left, 1),
    }


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class Graph:
    def __init__(self, token, base=None, timeout=60):
        self.token = token["access_token"]
        self.base = (base or os.environ.get("GRAPH_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # -- low level ---------------------------------------------------------- #
    def request(self, url, method="GET"):
        if not url.startswith("http"):
            url = self.base + ("" if url.startswith("/") else "/") + url
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            body = err.read()
            code, message = _error_fields(body)
            raise GraphError(err.code, code, message, url) from None
        except urllib.error.URLError as err:
            raise GraphError(0, "NetworkError", str(err.reason), url) from None

    def get_json(self, path):
        _, _, body = self.request(path)
        return json.loads(body) if body else {}

    # -- drive helpers ------------------------------------------------------ #
    def item(self, remote_path):
        """Metadata for a path relative to the drive root."""
        return self.get_json("/me/drive/root:/%s" % _quote_path(remote_path))

    def download(self, remote_path):
        """Raw bytes of a file at `remote_path` (drive root relative)."""
        _, headers, body = self.request("/me/drive/root:/%s:/content" % _quote_path(remote_path))
        return body, headers.get("Content-Type", "")

    def children(self, remote_path=""):
        """All direct children of a folder, following @odata.nextLink."""
        if remote_path:
            path = "/me/drive/root:/%s:/children" % _quote_path(remote_path)
        else:
            path = "/me/drive/root/children"
        out = []
        page = self.get_json(path)
        while True:
            out.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return out
            page = self.get_json(nxt)

    def walk(self, remote_path="", exts=(".md",), max_depth=20, max_files=None):
        """Yield (relative_path, item) lazily for every file under a folder."""
        exts = tuple(e.lower() for e in exts)
        seen = set()
        state = {"n": 0}

        def _walk(path, rel, depth):
            if depth > max_depth:  # cheap cycle guard
                return
            for item in self.children(path):
                name = item.get("name", "")
                child_remote = posixpath.join(path, name) if path else name
                child_rel = posixpath.join(rel, name) if rel else name
                if "folder" in item:
                    yield from _walk(child_remote, child_rel, depth + 1)
                elif name.lower().endswith(exts):
                    if child_rel in seen:
                        continue
                    if max_files is not None and state["n"] >= max_files:
                        return
                    seen.add(child_rel)
                    state["n"] += 1
                    yield child_rel, item

        yield from _walk(remote_path, "", 0)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _quote_path(remote_path):
    """Encode a drive path exactly once, tolerating an already-encoded input."""
    cleaned = remote_path.strip().strip("/")
    cleaned = urllib.parse.unquote(cleaned)   # idempotent: %20 -> space -> %20
    return urllib.parse.quote(cleaned, safe="/")


def _error_fields(body):
    try:
        doc = json.loads(body)
    except Exception:
        return "HttpError", body[:200].decode("utf-8", "replace")
    err = doc.get("error") or {}
    if isinstance(err, dict):
        return str(err.get("code", "HttpError")), str(err.get("message", ""))[:300]
    return "HttpError", str(err)[:200]


def ext_for(ctype, name=""):
    """Pick a file extension: response Content-Type first, then the name."""
    ct = (ctype or "").split(";")[0].strip().lower()
    if ct in CTYPE_EXT:
        return CTYPE_EXT[ct]
    if ct.startswith("text/"):
        return ".txt"
    base = posixpath.basename(name or "")
    if "." in base:
        return "." + base.rsplit(".", 1)[-1].lower()
    return ".bin"


def safe_relative_path(rel):
    """Reject anything that could escape the output directory."""
    raw = rel.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("refusing absolute path: %r" % rel)
    rel = raw.strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or rel.startswith("/"):
        raise ValueError("refusing unsafe path: %r" % rel)
    if not parts:
        raise ValueError("empty path")
    return os.path.join(*parts)


def is_text(ext, ctype=""):
    return ext.lower() in TEXT_EXTS or (ctype or "").lower().startswith("text/")
