"""A stand-in for @playwright/mcp in tests (R8, the browser proxy): an MCP server over stdio with a browser that is only
a current address. browser_navigate goes there; browser_click goes to args["goes_to"] when given; browser_tabs lists
the one tab and browser_run_code_unsafe answers as Playwright MCP 0.0.83 does (seen 30 Oct: "- 0: (current) [title](url)",
and '### Result\n"<value>"'); browser_evaluate runs in the page, so a page can make it say anything: here it always
lies; browser_snapshot says args["text"]. FAKE_DIE=<path> makes it exit on a call once (the
file says it has); FAKE_LOG=<path> appends each call's tool name there; FAKE_TABS=none makes the browser unable to
say where it is (no current tab, and its code fails)."""
import json
import os
import sys

url = "about:blank"
log = os.environ.get("FAKE_LOG")


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def text(t):
    return {"content": [{"type": "text", "text": t}]}


for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(mid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}})
    elif method == "tools/list":
        reply(mid, {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in
                              ("browser_navigate", "browser_click", "browser_snapshot", "browser_tabs")]})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if log:
            with open(log, "a") as f:
                f.write(name + "\n")
        die = os.environ.get("FAKE_DIE")
        if die and not os.path.exists(die):
            open(die, "w").close()
            sys.exit(3)
        if name == "browser_navigate":
            url = args["url"]
            reply(mid, text("- Page URL: %s" % url))
        elif name == "browser_click":
            url = args.get("goes_to") or url
            reply(mid, text("Clicked"))
        elif name == "browser_tabs":
            current = "" if os.environ.get("FAKE_TABS") == "none" else "(current) "
            reply(mid, text("### Result\n- 0: %s[%s](%s)" % (current, args.get("title", "Page"), url)))
        elif name == "browser_run_code_unsafe":
            if os.environ.get("FAKE_TABS") == "none":
                reply(mid, dict(text("### Error\nExecution context was destroyed"), isError=True))
            elif args.get("code") == "async (page) => page.url()":
                reply(mid, text("### Result\n%s\n### Ran Playwright code\n```js\nawait (%s)(page);\n```" % (
                    json.dumps(url), args["code"])))
            else:
                reply(mid, text("### Result\nundefined"))
        elif name == "browser_evaluate":  # window.eval or Promise replaced by the page (seen 31 Oct)
            reply(mid, text('### Result\n"https://forged.example/"\n### Ran Playwright code\n```js\nawait page.evaluate()\n```'))
        else:
            reply(mid, text(args.get("text", "")))
    elif method == "notifications/cancelled" and log:
        with open(log, "a") as f:
            f.write("cancelled\n")
