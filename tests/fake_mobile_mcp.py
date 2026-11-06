"""A stand-in for @mobilenext/mobile-mcp in tests (D1, the device proxy): an MCP server over stdio with devices that are
only the app in front of each. mobile_launch_app brings its package to the front; a tap goes to args["goes_to"] when
given (a link, a share sheet); mobile_get_foreground_app answers as Mobile MCP 1.0.16 does ("Foreground app: NAME
(PACKAGE), activity: CLASS"), with the app's own name, which FAKE_NAME sets (an app names itself); mobile_batch_commands
runs its steps as Mobile MCP does; mobile_list_available_devices lists FAKE_DEVICES (JSON), or one Android
emulator; other tools say args["text"]. FAKE_LOG=<path> appends each call's tool name there;
FAKE_ENV=<path> writes the telemetry and URL settings it was started with; FAKE_FRONT=none makes it unable to say."""
import json
import os
import sys

front = {}  # device -> package in front
log = os.environ.get("FAKE_LOG")
if os.environ.get("FAKE_ENV"):
    with open(os.environ["FAKE_ENV"], "w") as f:
        json.dump({k: os.environ.get(k) for k in ("MOBILEMCP_DISABLE_TELEMETRY", "MOBILEMCP_ALLOW_UNSAFE_URLS",
                                                  "MOBILEMCP_LEGACY_ROBOT")}, f)


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def text(t):
    return {"content": [{"type": "text", "text": t}]}


def run(name, args):
    device = args.get("device") or ""
    if name == "mobile_launch_app":
        front[device] = args["packageName"]
        return "Launched app %s" % args["packageName"]
    if name == "mobile_click_on_screen_at_coordinates":
        front[device] = args.get("goes_to") or front.get(device, "com.android.launcher3")
        return "Clicked on screen"
    if name == "mobile_list_available_devices":
        return json.dumps({"devices": json.loads(os.environ.get("FAKE_DEVICES") or "null") or [
            {"id": "emulator-5554", "name": "Pixel 9", "platform": "android", "type": "emulator", "state": "online"}]})
    return args.get("text", "")


for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(mid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mobile"}})
    elif method == "tools/list":
        reply(mid, {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in
                              ("mobile_launch_app", "mobile_click_on_screen_at_coordinates", "mobile_take_screenshot",
                               "mobile_get_foreground_app", "mobile_batch_commands")]})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if log:
            with open(log, "a") as f:
                f.write(name + "\n")
        if name == "mobile_get_foreground_app":
            if os.environ.get("FAKE_FRONT") == "none":
                reply(mid, dict(text("Getting the foreground app is not supported in legacy robot mode"), isError=True))
            else:
                package = front.get(args.get("device") or "", "com.android.launcher3")
                reply(mid, text("Foreground app: %s (%s), activity: %s.MainActivity" % (
                    os.environ.get("FAKE_NAME", "App"), package, package)))
        elif name == "mobile_batch_commands":
            out = []
            for i, step in enumerate(args.get("steps") or []):
                out.append("Step %d (%s): %s" % (i + 1, step["name"], run(step["name"], dict(
                    {"device": args.get("device")}, **(step.get("arguments") or {})))))
            reply(mid, text("\n".join(out)))
        else:
            reply(mid, text(run(name, args)))
