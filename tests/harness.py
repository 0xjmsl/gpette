"""Test a gpette config end to end, without XMTP.

    python tests/harness.py tests/scenarios/security.toml [--keep] [--only NAME]

A scenario file (TOML or JSON) holds:

    [harness]                 optional
    step_timeout = 240        seconds to wait for a reply
    no_reply_wait = 8         seconds of silence that count as "no reply"

    [config]                  a gpette.toml, written to a temp folder. The harness sets
                              daemon.url, server.host/port/data_dir and read_receipts;
                              relative paths are relative to the temp folder.

    [files]                   files to plant, path (relative to the temp folder) = text
    "work/bot/CLAUDE.md" = "You are a test bot."

    [[steps]]
    name = "picture"          optional label
    conversation = "c1"       conversation id
    sender = "inbox1"         sender inbox id
    kind = "dm"               dm | group
    text = "hello"            a text message, or:
    image = "pic.png"         an image (relative to the scenario file), sent as an
    caption = "What is it?"   xmtp_daemon attachment with this caption
    timeout = 300             optional, overrides step_timeout
    expect = [ {contains = "red"}, {not_contains = ["MANGO"]}, {no_denied = true} ]

Checks (text checks are case-insensitive; a list for contains / not_contains means
"all of them", contains_any means "at least one"):

    contains, contains_any, not_contains   on the reply text
    no_reply = true                        nothing is sent back
    denied_tool = "Read"                   a run summary lists a denied call of that tool
    no_denied = true                       no denied tool call
    max_cost_usd = 0.05                    the step's runs cost at most this

Strings in checks may use {tmp} (the temp folder), {tmp_name} (its folder name),
{user} (this machine's user name) and {home} (the user's home folder).

It starts a fake xmtp_daemon (records POST /send, answers /api/init and /health) and
gpette (`python main.py` with GPETTE_CONFIG), both on free 127.0.0.1 ports, POSTs each
step to /api/xmtp/incoming, and reads turns, cost and denied tools from the
"run summary" lines in the temp data/logs/app.log. Exit code 1 if any check fails.
"""
import argparse
import base64
import getpass
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- fake xmtp_daemon ----------------------------------------------------------------

class FakeDaemon:
    """Records every POST /send body; answers /api/init and /health."""

    def __init__(self):
        self.sent = []                     # (monotonic time, body)
        self.cond = threading.Condition()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/health"):
                    now = int(time.time() * 1000)
                    self._reply(200, {"address": "0xfake", "inboxId": "fakebot", "queued": 0,
                                      "streamStartedMs": now, "lastStreamEventMs": now})
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.path.startswith("/send"):
                    try:
                        body = json.loads(raw or b"{}")
                    except json.JSONDecodeError:
                        self._reply(400, {"error": "bad json"})
                        return
                    with daemon.cond:
                        daemon.sent.append((time.monotonic(), body))
                        daemon.cond.notify_all()
                    self._reply(200, {"id": uuid.uuid4().hex})
                elif self.path.startswith("/api/init"):
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def replies(self, conversation_id, since):
        with self.cond:
            return [b for t, b in self.sent
                    if t >= since and b.get("conversationId") == conversation_id
                    and b.get("contentType") != "readReceipt"]

    def wait_reply(self, conversation_id, since, timeout, quiet):
        """Wait for the first reply, then `quiet` seconds more for any follow-up."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while not self.replies_locked(conversation_id, since):
                left = deadline - time.monotonic()
                if left <= 0:
                    return []
                self.cond.wait(min(left, 1))
        time.sleep(quiet)
        return self.replies(conversation_id, since)

    def replies_locked(self, conversation_id, since):
        return [b for t, b in self.sent
                if t >= since and b.get("conversationId") == conversation_id
                and b.get("contentType") != "readReceipt"]

    def stop(self):
        self.server.shutdown()


# --- config ----------------------------------------------------------------------------

AVOID_PORTS = {3100, 3101, 4849, 4950, 8080}      # the usual live daemon / gpette / UI ports


def free_port():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in AVOID_PORTS:
            return port


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)               # a JSON string is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def dump_toml(data, prefix=""):
    """A small TOML writer: scalars, lists of scalars, nested tables, arrays of tables."""
    lines, tables, arrays = [], [], []
    for k, v in data.items():
        key = k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(v, dict):
            tables.append((name, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays.append((name, v))
        else:
            lines.append(f"{key} = {_toml_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for name, sub in tables:
        out += f"\n[{name}]\n" + dump_toml(sub, name)
    for name, items in arrays:
        for item in items:
            out += f"\n[[{name}]]\n" + dump_toml(item, name)
    return out


def load_scenario(path):
    with open(path, "rb") as f:
        if path.lower().endswith(".json"):
            return json.load(f)
        return tomllib.load(f)


def write_config(tmp, scenario, daemon_port, gpette_port):
    cfg = json.loads(json.dumps(scenario.get("config") or {}))       # deep copy
    cfg.setdefault("daemon", {})["url"] = f"http://127.0.0.1:{daemon_port}"
    server = cfg.setdefault("server", {})
    server.update({"host": "127.0.0.1", "port": gpette_port, "data_dir": "./data"})
    server.setdefault("read_receipts", False)
    cc = cfg.setdefault("claude_code", {})
    cc.setdefault("workdir", "./workdir")
    cc.setdefault("group_workdir", "./groups")
    cfg.setdefault("compaction", {}).setdefault("provider", "disabled")
    path = os.path.join(tmp, "gpette.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump_toml(cfg))
    for rel, text in (scenario.get("files") or {}).items():
        target = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
    for d in (cc["workdir"], cc["group_workdir"]):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    for p in (cfg.get("profiles") or {}).values():
        if p.get("workdir"):
            os.makedirs(os.path.join(tmp, p["workdir"]), exist_ok=True)
    return path


# --- gpette ----------------------------------------------------------------------------

def start_gpette(config_path, port, tmp):
    env = dict(os.environ, GPETTE_CONFIG=config_path, PYTHONIOENCODING="utf-8")
    out = open(os.path.join(tmp, "gpette_stdout.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=REPO, env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    proc._out = out
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out.flush()
            raise RuntimeError("gpette exited at start:\n" + _tail(out.name))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/", timeout=2) as r:
                if r.status == 200:
                    return proc
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("gpette did not start listening in 60 s:\n" + _tail(out.name))


def stop_gpette(proc):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    proc._out.close()


def _tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no output)"


def post_json(url, obj, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def envelope(step, base_dir):
    """One message in the xmtp_daemon webhook shape."""
    env = {
        "id": uuid.uuid4().hex,
        "senderInboxId": step["sender"],
        "timestamp": int(time.time() * 1000),
        "conversationId": step["conversation"],
        "role": "user",
        "kind": step.get("kind", "dm"),
        "members": [{"inboxId": step["sender"], "permissionLevel": "member", "identifiers": []}],
    }
    if step.get("image"):
        path = os.path.join(base_dir, step["image"])
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = os.path.basename(path)
        env.update({"contentType": "attachment", "content": data, "filename": name,
                    "mimeType": mime, "description": step.get("caption", ""),
                    "attachments": [{"content": data, "filename": name, "mimeType": mime}]})
    else:
        env.update({"contentType": "text", "content": step.get("text", "")})
    return env


# --- run summaries from the log ---------------------------------------------------------

_SUMMARY = re.compile(r"run summary conv=(\S+) profile=(\S+) turns=(\S+) cost_usd=(\S+) "
                      r"duration_ms=(\S+) denied=\[(.*)\]\s*$")


def read_summaries(log_path, offset, conversation_id):
    """Run-summary lines for this conversation written after `offset`."""
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    runs = []
    for line in text.splitlines():
        m = _SUMMARY.search(line)
        if not m or m.group(1) != conversation_id:
            continue
        denied = m.group(6).strip()
        runs.append({
            "profile": m.group(2),
            "turns": None if m.group(3) == "None" else int(m.group(3)),
            "cost": 0.0 if m.group(4) == "None" else float(m.group(4)),
            "denied": [] if denied in ("", "none") else re.findall(r"(\w[\w-]*)\(", denied),
            "denied_raw": "" if denied == "none" else denied,
        })
    return runs


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --- checks ----------------------------------------------------------------------------

def _as_list(v):
    return v if isinstance(v, list) else [v]


def evaluate(expect, reply, runs, subs):
    """Returns a list of failure strings (empty = pass)."""
    fails = []
    low = reply.lower()
    sub = lambda s: str(s).format(**subs)
    cost = sum(r["cost"] for r in runs)
    denied = [d for r in runs for d in r["denied"]]
    for check in expect:
        for kind, val in check.items():
            if kind == "contains":
                for s in _as_list(val):
                    if sub(s).lower() not in low:
                        fails.append(f"missing '{sub(s)}'")
            elif kind == "contains_any":
                opts = [sub(s) for s in _as_list(val)]
                if not any(o.lower() in low for o in opts):
                    fails.append(f"none of {opts}")
            elif kind == "not_contains":
                for s in _as_list(val):
                    if sub(s).lower() in low:
                        fails.append(f"has '{sub(s)}'")
            elif kind == "no_reply":
                if val and reply:
                    fails.append("got a reply")
                if not val and not reply:
                    fails.append("no reply")
            elif kind == "denied_tool":
                for t in _as_list(val):
                    if t not in denied:
                        fails.append(f"no denied {t}")
            elif kind == "no_denied":
                if val and denied:
                    fails.append(f"denied {denied}")
            elif kind == "max_cost_usd":
                if cost > float(val):
                    fails.append(f"cost {cost:.4f} > {val}")
            else:
                fails.append(f"unknown check '{kind}'")
    return fails


def short(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


# --- main ------------------------------------------------------------------------------

def run(scenario_path, keep=False, only=None):
    scenario = load_scenario(scenario_path)
    base_dir = os.path.dirname(os.path.abspath(scenario_path))
    opts = scenario.get("harness") or {}
    step_timeout = float(opts.get("step_timeout", 240))
    no_reply_wait = float(opts.get("no_reply_wait", 8))
    quiet = float(opts.get("quiet_secs", 1.5))

    tmp = tempfile.mkdtemp(prefix="gpette_")
    subs = {"tmp": tmp, "tmp_name": os.path.basename(tmp), "user": getpass.getuser(),
            "home": os.path.expanduser("~")}
    daemon = FakeDaemon()
    gpette_port = free_port()
    proc = None
    results = []
    try:
        config_path = write_config(tmp, scenario, daemon.port, gpette_port)
        proc = start_gpette(config_path, gpette_port, tmp)
        log_path = os.path.join(tmp, "data", "logs", "app.log")
        print(f"gpette on 127.0.0.1:{gpette_port}, fake daemon on 127.0.0.1:{daemon.port}, temp {tmp}")
        url = f"http://127.0.0.1:{gpette_port}/api/xmtp/incoming"
        for i, step in enumerate(scenario.get("steps") or [], 1):
            name = step.get("name") or f"step{i}"
            if only and only not in name:
                continue
            expect = step.get("expect") or []
            wants_silence = any(c.get("no_reply") for c in expect)
            offset = log_size(log_path)
            since = time.monotonic()
            started = time.time()
            post_json(url, envelope(step, base_dir))
            if wants_silence:
                time.sleep(no_reply_wait)
                bodies = daemon.replies(step["conversation"], since)
            else:
                bodies = daemon.wait_reply(step["conversation"], since,
                                           float(step.get("timeout", step_timeout)), quiet)
            reply = "\n".join(b["content"] if isinstance(b.get("content"), str)
                              else json.dumps(b.get("content")) for b in bodies)
            runs = read_summaries(log_path, offset, step["conversation"])
            fails = evaluate(expect, reply, runs, subs)
            if not bodies and not wants_silence:
                fails.insert(0, "timeout: no reply")
            results.append({"name": name, "ok": not fails, "fails": fails, "reply": reply,
                            "runs": runs, "secs": time.time() - started})
            r = results[-1]
            print(f"  {'PASS' if r['ok'] else 'FAIL'} {name} ({r['secs']:.0f}s)", flush=True)
    finally:
        stop_gpette(proc)
        daemon.stop()
        print_table(results)
        if keep:
            print(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return all(r["ok"] for r in results) and bool(results)


def print_table(results):
    if not results:
        print("no steps ran")
        return
    print()
    print(f"{'step':<22} {'result':<6} {'turns':>5} {'cost':>8}  {'denied':<18} reply / why")
    print("-" * 110)
    total = 0.0
    for r in results:
        cost = sum(x["cost"] for x in r["runs"])
        total += cost
        turns = "+".join(str(x["turns"]) for x in r["runs"]) or "-"
        denied = ",".join(d for x in r["runs"] for d in x["denied"]) or "-"
        print(f"{r['name'][:22]:<22} {'pass' if r['ok'] else 'FAIL':<6} {turns:>5} "
              f"{cost:>8.4f}  {short(denied, 18):<18} {short(r['reply'], 60) or '(no reply)'}")
        for f in r["fails"]:
            print(f"{'':<22} {'':<6} {'':>5} {'':>8}  {'':<18} ! {f}")
    print("-" * 110)
    passed = sum(r["ok"] for r in results)
    print(f"{passed}/{len(results)} passed, total cost ${total:.4f}")


def main():
    ap = argparse.ArgumentParser(description="Run a gpette test scenario.")
    ap.add_argument("scenario")
    ap.add_argument("--keep", action="store_true", help="keep the temp folder (config, data, logs)")
    ap.add_argument("--only", help="run only steps whose name contains this text")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(0 if run(args.scenario, args.keep, args.only) else 1)


if __name__ == "__main__":
    main()
