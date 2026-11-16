"""Command line interface for graph2md."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .graph import (
    Graph,
    GraphError,
    ext_for,
    is_text,
    load_token,
    safe_relative_path,
    token_status,
)

DESCRIPTION = """\
Download Markdown (and other text files) from OneDrive/SharePoint via Microsoft Graph.

Examples
  graph2md status
  graph2md get  "Obsidian/Workplace/Notes/Daily/2026-11-16.md"
  graph2md get  "Notes/2026-11-16.md" -o today.md --force
  graph2md tree "Obsidian/Workplace/Personal" --out ./vault-copy
  graph2md list "Obsidian/Workplace/Personal" --ext .md .txt
"""


def build_parser():
    # Global flags are accepted both before and after the subcommand, so
    # `graph2md get x.md --token-file t.json` works like the obvious thing.
    p = argparse.ArgumentParser(
        prog="graph2md",
        description="Download Markdown from OneDrive/SharePoint through Microsoft Graph.",
        epilog=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--token-file", help="path to a JSON envelope or a raw JWT (or the token itself)")
    p.add_argument("--base", default=os.environ.get("GRAPH_BASE", "https://graph.microsoft.com/v1.0"),
                   help="override the Graph base URL")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--quiet", action="store_true", help="only errors")
    p.add_argument("--version", action="version", version="graph2md " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status",
                        help="show token identity, expiry and scopes (never the token)")
    st.set_defaults(func=cmd_status)

    g = sub.add_parser("get", help="download one file")
    g.add_argument("path", help="path relative to the drive root, e.g. Notes/a.md")
    g.add_argument("-o", "--out", help="destination file (default: basename in cwd)")
    g.add_argument("--force", action="store_true", help="overwrite an existing file")
    g.add_argument("--raw", action="store_true", help="strip nothing; keep bytes exactly as served")
    g.set_defaults(func=cmd_get)

    t = sub.add_parser("tree", help="download a whole folder recursively")
    t.add_argument("path", help="folder path relative to the drive root")
    t.add_argument("--out", default=".", help="local output directory (default: .)")
    t.add_argument("--ext", nargs="+", default=[".md"], help="extensions to fetch (default: .md)")
    t.add_argument("--force", action="store_true", help="overwrite existing files")
    t.add_argument("--dry-run", action="store_true", help="list what would be downloaded")
    t.add_argument("--max-depth", type=int, default=20, help="folder recursion depth (default: 20)")
    t.add_argument("--limit", type=int, default=None, help="stop after N matching files")
    t.set_defaults(func=cmd_tree)

    ls = sub.add_parser("list", help="list files in a folder")
    ls.add_argument("path", nargs="?", default="", help="folder path (default: drive root)")
    ls.add_argument("--ext", nargs="+", default=None, help="filter by extension")
    ls.add_argument("--long", action="store_true", help="include size and modified time")
    ls.set_defaults(func=cmd_list)

    return p


GLOBAL_FLAGS = {"--token-file": 1, "--base": 1, "--json": 0, "--quiet": 0, "--version": 0}


def normalize_argv(argv):
    """Move global flags in front of the subcommand.

    argparse only accepts parent-parser options *before* the subcommand, which
    makes `graph2md get x.md --token-file t.json` fail confusingly. Reordering
    first means both spellings work.
    """
    globals_, rest, i = [], [], 0
    while i < len(argv):
        arg = argv[i]
        key = arg.split("=", 1)[0]
        if key in GLOBAL_FLAGS:
            take = GLOBAL_FLAGS[key]
            if "=" in arg or take == 0:
                globals_.append(arg)
                i += 1
            else:
                globals_.extend(argv[i:i + 1 + take])
                i += 1 + take
            continue
        rest.append(arg)
        i += 1
    return globals_ + rest


def _token(args, parser):
    try:
        return load_token(args.token_file)
    except GraphError as err:
        parser.error(str(err))


def cmd_status(args, parser):
    token = _token(args, parser)
    st = token_status(token)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    hours = st["hours_left"]
    state = "unknown"
    if hours is not None:
        state = "EXPIRED" if hours <= 0 else "valid"
    print("user      : %s" % (st["upn"] or "?"))
    print("app       : %s" % (st["appid"] or "?"))
    print("expires   : %s (%s%s)" % (
        st["expires_on"] or "unknown", state,
        "" if hours is None else ", %.1fh left" % hours))
    print("scopes    : %s" % (st["scopes"] or "unknown"))
    return 0 if (hours is None or hours > 0) else 3


def cmd_get(args, parser):
    graph = Graph(_token(args, parser), base=args.base)
    body, ctype = graph.download(args.path)
    out = args.out or os.path.basename(args.path.rstrip("/"))
    if not out:
        out = "download" + ext_for(ctype, args.path)
    if os.path.exists(out) and not args.force:
        parser.error("%s exists (use --force)" % out)
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    ext = ext_for(ctype, out)
    payload = body
    if not args.raw and is_text(ext, ctype):
        text = body.decode("utf-8-sig", "replace")
        payload = text.encode("utf-8")
    with open(out, "wb") as fh:
        fh.write(payload)
    if args.json:
        print(json.dumps({"path": args.path, "out": out, "bytes": len(body),
                          "content_type": ctype, "ext": ext}, indent=2))
    elif not args.quiet:
        print("%s -> %s (%d bytes, %s)" % (args.path, out, len(body), ctype or "?"))
    return 0


def cmd_tree(args, parser):
    graph = Graph(_token(args, parser), base=args.base)
    root = os.path.abspath(args.out)
    files = graph.walk(args.path, exts=args.ext, max_depth=args.max_depth, max_files=args.limit)
    if args.dry_run:
        n = 0
        for rel, item in files:
            n += 1
            print(rel, flush=True)
        if not args.quiet:
            print("--- %d file(s) would be downloaded" % n, file=sys.stderr)
        return 0

    done, skipped = 0, 0
    for rel, item in files:
        try:
            local = os.path.join(root, safe_relative_path(rel))
        except ValueError as err:
            print("skip: %s" % err, file=sys.stderr)
            continue
        if os.path.exists(local) and not args.force:
            skipped += 1
            if not args.quiet:
                print("skip (exists): %s" % rel)
            continue
        remote = os.path.join(args.path.strip("/"), rel) if args.path.strip("/") else rel
        body, ctype = graph.download(remote.replace(os.sep, "/"))
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(body.decode("utf-8-sig", "replace").encode("utf-8") if is_text(rel, ctype) else body)
        done += 1
        if not args.quiet:
            print("%s (%d bytes)" % (rel, len(body)))
    if not args.quiet:
        print("--- %d downloaded, %d skipped -> %s" % (done, skipped, root), file=sys.stderr)
    return 0


def cmd_list(args, parser):
    graph = Graph(_token(args, parser), base=args.base)
    rows = []
    for item in graph.children(args.path):
        name = item.get("name", "")
        if "folder" in item:
            kind = "dir"
        else:
            kind = "file"
            if args.ext and not name.lower().endswith(tuple(e.lower() for e in args.ext)):
                continue
        rows.append({"name": name, "type": kind,
                     "size": item.get("size", 0),
                     "modified": item.get("lastModifiedDateTime", "")})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        if args.long:
            print("%-8s %12d %s %s" % (r["type"], r["size"], r["modified"][:19], r["name"]))
        else:
            print(("%s/" % r["name"]) if r["type"] == "dir" else r["name"])
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    try:
        return args.func(args, parser)
    except GraphError as err:
        print("error: %s" % err, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
