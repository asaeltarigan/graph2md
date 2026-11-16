# graph2md

Download Markdown — and any other text file — from **OneDrive / SharePoint** through the
Microsoft Graph API, from a terminal, in one command.

Built for the boring case that Graph makes awkward: *"my notes live in OneDrive, I want
them as `.md` files on this machine, in the same folder structure."* No SDK, no MSAL, no
dependencies — a Python access token in, files on disk out.

```
graph2md tree "Obsidian/Workplace/Workplace/Personal" --out ./vault-copy
--- 42 downloaded, 0 skipped -> /home/you/vault-copy
```

## Why this exists

Microsoft Graph can serve you a Markdown file, but only after you assemble
`/me/drive/root:/<double-encoded path>:/content`, attach a bearer token, follow a redirect
to a short-lived pre-authenticated URL, and remember that Windows editors love to prepend a
UTF-8 BOM to token files. Every one of those steps fails in a way that looks like a bad
token. This tool does the four things that actually matter:

- **Encodes the path once.** `Finished Tasks.md` and `Finished%20Tasks.md` both work.
- **Reads whatever token file you have.** JSON envelope (`{"access_token": ...}`), a raw
  JWT, a token with a BOM, a quoted string, a pasted `Bearer ` prefix.
- **Recurses folders** and keeps the tree shape, including emoji folder names.
- **Refuses to be clever.** No overwrite without `--force`, no path escape, no silent
  token printing.

## Install

```bash
git clone https://github.com/asaeltarigan/graph2md.git
cd graph2md
pipx install .          # or: pip install --user .
graph2md --help
```

No runtime dependencies. Python 3.8+.

## Get a token

`graph2md` does not do OAuth. You bring a token, which is the right split: app
registration and consent belong to you, not to a downloader.

1. Open <https://developer.microsoft.com/en-us/graph/graph-explorer> and sign in.
2. Select the **Access token** tab and copy the token.
3. Save it — an envelope is nicest, a bare JWT is fine:

```bash
# raw JWT
pbpaste > ~/.graph_token

# or the envelope form some tools already use
python3 - <<'PY'
import json, pathlib
tok = pathlib.Path("token.txt").read_text().strip()
pathlib.Path.home().joinpath(".onedrive").mkdir(exist_ok=True)
pathlib.Path.home().joinpath(".onedrive/graph_token.json").write_text(json.dumps({
    "access_token": tok,
    "expires_on": "2026-11-17T08:08:16Z",
}))
PY
```

4. Then either pass `--token-file` or export it:

```bash
export GRAPH_TOKEN_FILE=~/.graph_token
graph2md status
```

```
user      : you@example.com
app       : de8bc8b5-...
expires   : 2026-11-17T08:08:16+00:00 (valid, 23.6h left)
scopes    : Files.ReadWrite.All Notes.ReadWrite.All ...
```

Graph Explorer tokens last about 24 hours and cannot be refreshed — `graph2md status` and
exit code `3` tell you when one has died so you never debug a 401 blind.

## Usage

```
graph2md [--token-file F] [--base URL] [--json] [--quiet] <command> ...

status                      token identity, expiry, scopes (never the token)
get  <path> [-o OUT] [--force] [--raw]
list [<folder>] [--ext ...] [--long]
tree <folder> [--out DIR] [--ext .md .txt] [--force] [--dry-run]
              [--max-depth N] [--limit N]
```

Global flags work before or after the command: `graph2md get a.md --token-file t.json`
parses the same as `graph2md --token-file t.json get a.md`.

### One file

```bash
graph2md get "Obsidian/Workplace/Notes/2026-11-16.md"
graph2md get "Notes/daily.md" -o today.md --force
graph2md get "Notes/daily.md" --json          # path/bytes/content_type, for scripts
```

Text files are written as UTF-8 with any BOM stripped; `--raw` keeps the bytes exactly as
the server sent them.

### A whole folder

```bash
graph2md tree "Obsidian/Workplace/Personal" --out ./personal --ext .md .txt
graph2md tree "Obsidian/Workplace/Personal" --dry-run | head   # preview, downloads nothing
```

Existing files are skipped unless `--force`, so re-running after a sync is safe. `--limit`
and `--max-depth` make a huge vault interruptible instead of a five-minute mystery.

### Browsing

```bash
graph2md list                       # drive root
graph2md list "Obsidian" --long     # sizes + modified times
```

### In a script

```bash
export GRAPH_TOKEN_FILE=~/.graph_token
if graph2md status --quiet; then
    graph2md tree "Obsidian/Workplace/Workplace" --out ~/vault-backup --force
fi
```

## Exit codes

| code | meaning |
|-----:|---------|
| 0 | success |
| 1 | Graph error (401, 403, 404 … printed with a hint) |
| 2 | usage / local problem (no token source, file exists without `--force`) |
| 3 | `status` only: the token has already expired |

## Notes and limits

- **Delegated auth only.** The token must be a user token for the drive you want; app-only
  (client credentials) would need a `/users/{id}/drive` base that this CLI does not build.
- **OneDrive personal + SharePoint document libraries** both work: pass `--base` with a
  site-scoped root if your files are not in the signed-in user's OneDrive.
- **`.md` is not special to Graph.** It serves whatever bytes exist; `tree` filters by
  extension locally, so `--ext .md .txt .csv .json` is all it takes to widen the net.
- **Downloads are read-only.** Nothing here writes to your drive.

## Tests

22 offline tests, no Microsoft account needed — a fake Graph server on `127.0.0.1` serves
listings, paginated folders, and files, so the whole CLI is exercised end to end:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
