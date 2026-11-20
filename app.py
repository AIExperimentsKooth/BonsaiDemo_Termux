#!/usr/bin/env python3
"""
Minimal agentic harness for a llama.cpp server (OpenAI-compatible API).

Serves a browser UI and runs a tool-using loop against --base-url
(default http://127.0.0.1:8080, the Bonsai llama-server on this phone).

It can also swap the loaded GGUF: /api/switch stops the running llama-server,
waits for it to exit (so its RAM is really released), starts the demo repo's
start_llama_server.sh with BONSAI_GGUF pointing at the chosen file, and polls
/health until the new model answers. Switching is refused while a turn runs.

Standard library only - nothing to pip install.

    python3 app.py --host 0.0.0.0 --port 8081
    # then browse http://<phone-ip>:8081 from another machine
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = (
    "You are Bonsai Agent, a careful assistant running on an Android phone.\n"
    "You act by calling exactly one tool per turn.\n"
    "Reply with a single JSON object and nothing else - no prose, no markdown.\n"
    "Tools:\n"
    '  {"tool":"list_dir","path":"."}\n'
    '  {"tool":"read_file","path":"notes.txt"}\n'
    '  {"tool":"write_file","path":"out.txt","content":"text"}\n'
    '  {"tool":"patch","path":"out.txt","find":"exact old text","replace":"new text"}\n'
    '  {"tool":"patch","path":"out.txt","edits":[{"find":"a","replace":"b"},{"find":"c","replace":"d"}]}\n'
    '  {"tool":"run_bash","command":"ls -la"}\n'
    '  {"tool":"finish","answer":"what you did or found"}\n'
    "Rules: paths are relative to the workspace; one action per turn; "
    "never invent file contents you have not read; call finish when done.\n"
    "To change part of an existing file use patch, not write_file: read the file, "
    "then copy the exact text to replace. 'find' must match exactly once (include "
    "surrounding lines for context), and nothing is written unless every edit matches.\n"
)

TOOL_NAMES = ["list_dir", "read_file", "write_file", "patch", "run_bash", "finish"]
OBS_CHARS = 1200      # per-message cap when re-sending history
READ_CHARS = 6000     # cap for read_file output
BASH_CHARS = 4000     # cap for run_bash output
BASH_TIMEOUT = 60

# Selectable models. Override with --models '<json list>' if these move.
DEFAULT_MODELS = [
    {
        "id": "bonsai2-27b",
        "label": "Ternary-Bonsai-2-27B (PTQ1_0)",
        "path": "~/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    },
    {
        "id": "abliterated-v2-27b",
        "label": "Ternary-Bonsai-2-27B-Abliterated-v2 (PTQ1_0)",
        "path": "~/Bonsai-demo/models/abliterated/Ternary-Bonsai-2-27B-Abliterated-v2-PTQ1_0.gguf",
    },
]

DEFAULT_SERVER_SCRIPT = "~/Bonsai-demo/scripts/start_llama_server.sh"
DEFAULT_SERVER_LOG = "~/llama-server.log"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class WorkspaceError(Exception):
    pass


def resolve_in_workspace(ws, path):
    if path is None or str(path).strip() == "":
        path = "."
    candidate = os.path.realpath(os.path.join(ws, str(path)))
    root = os.path.realpath(ws)
    if candidate != root and not candidate.startswith(root + os.sep):
        raise WorkspaceError("path escapes the workspace: %s" % path)
    return candidate


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def tool_list_dir(ws, path="."):
    target = resolve_in_workspace(ws, path)
    if not os.path.isdir(target):
        return "not a directory: %s" % path
    out = []
    for name in sorted(os.listdir(target))[:200]:
        full = os.path.join(target, name)
        if os.path.isdir(full):
            out.append(name + "/")
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            out.append("%s (%s bytes)" % (name, size))
    return "\n".join(out) if out else "(empty directory)"


def tool_read_file(ws, path, limit=READ_CHARS):
    target = resolve_in_workspace(ws, path)
    if not os.path.isfile(target):
        return "no such file: %s" % path
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        return "read failed: %s" % exc
    if len(data) > limit:
        data = data[:limit] + "\n...[truncated]"
    return data if data.strip() else "(empty file)"


def tool_write_file(ws, path, content):
    target = resolve_in_workspace(ws, path)
    text = content if isinstance(content, str) else json.dumps(content)
    parent = os.path.dirname(target) or ws
    os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return "wrote %d bytes to %s" % (len(text), os.path.relpath(target, ws))


def _line_count(text):
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _patch_hint(text, find):
    """Explain why an exact 'find' block did not match."""
    lines = [ln for ln in find.splitlines() if ln.strip()]
    if not lines:
        return ""
    needle = lines[0].strip()
    hits = [i for i, ln in enumerate(text.splitlines(), 1) if needle in ln]
    if not hits:
        return " (its first line %r appears nowhere in the file either)" % needle[:80]
    return (
        " (its first line %r appears on line%s %s - check indentation and exact whitespace)"
        % (needle[:80], "" if len(hits) == 1 else "s", ", ".join(str(h) for h in hits[:5]))
    )


def tool_patch(ws, path, find=None, replace=None, edits=None, replace_all=False):
    """Replace exact snippets in an existing file.

    Sending only the changed text keeps a fix small instead of resending the
    whole file. Every edit must match; the file is written only if all succeed.
    """
    target = resolve_in_workspace(ws, path)
    if not os.path.isfile(target):
        return "no such file: %s (use write_file to create a new file)" % path
    try:
        with open(target, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return "read failed: %s" % exc
    if b"\x00" in raw:
        return "refusing to patch a binary file: %s" % path
    text = raw.decode("utf-8", "replace")

    ops = []
    if isinstance(edits, list) and edits:
        for i, item in enumerate(edits):
            if not isinstance(item, dict):
                return "edits[%d] must be an object with find/replace" % i
            if not isinstance(item.get("find"), str) or item["find"] == "":
                return "edits[%d]: 'find' must be a non-empty string" % i
            rep = item.get("replace", "")
            ops.append((item["find"], "" if rep is None else str(rep)))
    elif find is not None:
        if not isinstance(find, str) or find == "":
            return "'find' must be a non-empty string"
        if replace is None:
            return 'add "replace": "" to delete the matched text'
        ops.append((find, str(replace)))
    else:
        return (
            "patch needs 'find' and 'replace' (or an 'edits' list of them); "
            "read the file first so you can copy the exact text"
        )

    updated = text
    for n, (needle, rep) in enumerate(ops, 1):
        count = updated.count(needle)
        if count == 0:
            return "edit %d: text not found%s\nno changes written" % (
                n, _patch_hint(updated, needle)
            )
        if count > 1 and not replace_all:
            return (
                "edit %d: 'find' matches %d times - include more surrounding context, "
                "or pass \"replace_all\":true\nno changes written" % (n, count)
            )
        updated = updated.replace(needle, rep)

    if updated == text:
        return "patch matched but left %s unchanged" % path
    try:
        # newline="" keeps the file's existing line endings byte-for-byte.
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
    except OSError as exc:
        return "write failed: %s" % exc
    return "patched %s: %d edit%s, %d -> %d bytes, %d -> %d lines" % (
        os.path.relpath(target, ws), len(ops), "" if len(ops) == 1 else "s",
        len(text), len(updated), _line_count(text), _line_count(updated),
    )


def tool_run_bash(ws, command, timeout=BASH_TIMEOUT):
    if not command or not str(command).strip():
        return "no command given"
    try:
        proc = subprocess.run(
            str(command), shell=True, cwd=ws, capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timed out after %ss" % timeout
    except OSError as exc:
        return "could not run: %s" % exc
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
    if len(out) > BASH_CHARS:
        out = out[:BASH_CHARS] + "\n...[truncated]"
    return "exit %d\n%s" % (proc.returncode, out)


def execute_tool(ws, name, action, allow_bash=True):
    try:
        if name == "list_dir":
            return tool_list_dir(ws, action.get("path", "."))
        if name == "read_file":
            return tool_read_file(ws, action.get("path", ""))
        if name == "write_file":
            return tool_write_file(ws, action.get("path", ""), action.get("content", ""))
        if name == "patch":
            return tool_patch(
                ws, action.get("path", ""),
                find=action.get("find"),
                replace=action.get("replace"),
                edits=action.get("edits"),
                replace_all=bool(action.get("replace_all") or action.get("all")),
            )
        if name == "run_bash":
            if not allow_bash:
                return "run_bash is disabled on this harness (start it without --no-bash)"
            return tool_run_bash(ws, action.get("command", ""))
        return "unknown tool: %r (known: %s)" % (name, ", ".join(TOOL_NAMES))
    except WorkspaceError as exc:
        return "error: %s" % exc
    except Exception as exc:  # noqa: BLE001 - tools must never kill the loop
        return "error: %s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------
# model plumbing
# --------------------------------------------------------------------------

def stream_chat(base_url, messages, max_tokens, temperature, timeout):
    """Yield content deltas from an OpenAI-compatible /v1/chat/completions."""
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece


def extract_action(text):
    """Return the first balanced JSON object in text, or None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


def compact(messages):
    """Keep the system/task head plus the last few turns, keeping the prompt small."""
    head = messages[:2]
    tail = messages[2:][-4:]
    out = list(head)
    for msg in tail:
        content = msg.get("content") or ""
        if len(content) > OBS_CHARS * 2:
            content = content[: OBS_CHARS * 2] + "\n...[truncated]"
        out.append({"role": msg["role"], "content": content})
    return out


def event(kind, **fields):
    payload = {"type": kind}
    payload.update(fields)
    return payload


def run_agent(cfg, task, max_steps, max_tokens):
    """Generator of events for one agent run."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    for step in range(1, max_steps + 1):
        yield event("step", n=step, of=max_steps)
        buf = ""
        try:
            for piece in stream_chat(cfg.base_url, compact(messages), max_tokens,
                                     cfg.temperature, cfg.timeout):
                buf += piece
                yield event("assistant_delta", text=piece)
        except Exception as exc:  # noqa: BLE001
            yield event("error", message="model request failed: %s" % exc)
            return

        yield event("assistant", text=buf)
        action = extract_action(buf)
        if action is None:
            yield event("final", text=buf.strip() or "(empty reply)")
            return

        name = str(action.get("tool") or action.get("action") or "").strip().lower()
        if name in ("finish", "final", "answer"):
            answer = action.get("answer") or action.get("text") or buf.strip()
            yield event("final", text=str(answer))
            return
        if not name:
            yield event("final", text=buf.strip())
            return

        args = {k: v for k, v in action.items() if k not in ("tool", "action")}
        yield event("tool", name=name, args=args)
        result = execute_tool(cfg.workspace, name, action, cfg.allow_bash)
        yield event("observation", name=name, text=result, truncated=len(result) > OBS_CHARS)
        if len(result) > OBS_CHARS:
            result = result[:OBS_CHARS] + "\n...[truncated]"

        messages.append({"role": "assistant", "content": buf})
        messages.append({"role": "user", "content": "OBSERVATION:\n" + result})

    yield event("final", text="stopped: reached the %d-step limit without finish" % max_steps)


# --------------------------------------------------------------------------
# model server: probe, unload, load, switch
# --------------------------------------------------------------------------

class ModelManager:
    """Owns the llama-server process: which GGUF is loaded and swapping it."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.turns = 0              # turns currently streaming
        self.switching = False
        self.current = None         # model id, None when nothing is loaded
        self.proc = None
        self.loaded_seconds = None

    # -- helpers -----------------------------------------------------------
    def model(self, mid):
        for m in self.cfg.models:
            if m["id"] == mid:
                return m
        return None

    def healthy(self, timeout=5):
        try:
            with urllib.request.urlopen(
                self.cfg.base_url.rstrip("/") + "/health", timeout=timeout
            ) as resp:
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False

    def props(self):
        try:
            with urllib.request.urlopen(
                self.cfg.base_url.rstrip("/") + "/props", timeout=10
            ) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return None

    def detect_current(self):
        """Work out which registry entry the running server has loaded."""
        props = self.props()
        if not props:
            self.current = None
            return None
        loaded = os.path.basename((props.get("model_path") or "").replace("\\", "/"))
        for m in self.cfg.models:
            if os.path.basename(m["path"]) == loaded:
                self.current = m["id"]
                return self.current
        self.current = None
        return None

    def server_pids(self):
        """PIDs whose argv[0]/argv[1] is llama-server (never our own process)."""
        pids = []
        me = os.getpid()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return pids
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == me:
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            argv = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
            if not argv:
                continue
            if any(os.path.basename(a) == "llama-server" for a in argv[:2]):
                pids.append(pid)
        return pids

    def log_tail(self, limit=1500):
        try:
            with open(self.cfg.server_log, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError as exc:
            return "(could not read %s: %s)" % (self.cfg.server_log, exc)

    # -- state exposed to the UI ------------------------------------------
    def models_payload(self):
        models = []
        for m in self.cfg.models:
            present = os.path.isfile(m["path"])
            size_mb = None
            if present:
                try:
                    size_mb = int(os.path.getsize(m["path"]) // (1024 * 1024))
                except OSError:
                    pass
            models.append({
                "id": m["id"],
                "label": m["label"],
                "path": m["path"],
                "present": present,
                "size_mb": size_mb,
            })
        if self.current is None and self.healthy(2):
            self.detect_current()
        return {
            "models": models,
            "current": self.current,
            "switching": self.switching,
            "busy": self.turns > 0,
            "server_running": bool(self.server_pids()),
            "loaded_seconds": self.loaded_seconds,
        }

    # -- turn bookkeeping --------------------------------------------------
    def begin_turn(self):
        with self.lock:
            if self.switching:
                return "a model switch is in progress; wait for it to finish"
            self.turns += 1
            return None

    def end_turn(self):
        with self.lock:
            self.turns = max(0, self.turns - 1)

    # -- unload ------------------------------------------------------------
    def unload(self):
        pids = self.server_pids()
        if not pids:
            return True, "no model server was running"
        log("unloading model server pids=%s" % pids)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                log("SIGTERM %d failed: %s" % (pid, exc))

        deadline = time.time() + self.cfg.unload_timeout
        while time.time() < deadline:
            if not self.server_pids() and not self.healthy(2):
                return True, "previous model unloaded"
            time.sleep(1.0)

        for pid in self.server_pids():
            log("SIGKILL %d" % pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.time() + 20
        while time.time() < deadline:
            if not self.server_pids():
                break
            time.sleep(0.5)
        if self.server_pids():
            return False, "could not stop the running model server (pids %s)" % self.server_pids()
        return True, "previous model force-stopped"

    # -- load --------------------------------------------------------------
    def start(self, model):
        port = urlsplit(self.cfg.base_url).port or 8080
        env = dict(os.environ)
        env["BONSAI_GGUF"] = model["path"]
        env["BONSAI_HOST"] = urlsplit(self.cfg.base_url).hostname or "127.0.0.1"
        env["PORT"] = str(port)
        env["PATH"] = env.get("PATH") or "/data/data/com.termux/files/usr/bin:/system/bin:/usr/bin:/bin"
        script = self.cfg.server_script
        out = open(self.cfg.server_log, "ab", buffering=0)
        try:
            self.proc = subprocess.Popen(
                ["sh", script],
                cwd=os.path.dirname(script) or ".",
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return "could not start %s: %s" % (script, exc)
        finally:
            out.close()
        log("started model server pid=%s model=%s" % (self.proc.pid, model["path"]))
        return None

    # -- switch (generator of SSE events) ----------------------------------
    def switch_events(self, mid):
        model = self.model(mid)
        if model is None:
            yield event("switch_error", message="unknown model: %r" % mid)
            return

        with self.lock:
            if self.switching:
                yield event("switch_error", message="another model switch is already running")
                return
            if self.turns:
                yield event("switch_error", message="a turn is running - stop it before switching models")
                return
            self.switching = True

        try:
            if not os.path.isfile(model["path"]):
                yield event("switch_error", message="model file is not downloaded: %s" % model["path"])
                return
            if self.current == mid and self.healthy():
                self.loaded_seconds = 0.0
                yield event("switch_ready", model=mid, label=model["label"], seconds=0.0,
                            note="already loaded")
                return

            yield event("switch_phase", phase="unloading",
                        message="stopping the running model server")
            ok, msg = self.unload()
            if not ok:
                yield event("switch_error", message=msg)
                return
            self.current = None
            yield event("switch_phase", phase="unloaded", message=msg)
            # Give Android a moment to reclaim the freed pages before loading.
            time.sleep(2.0)

            yield event("switch_phase", phase="loading", model=mid, label=model["label"],
                        message="loading %s (takes minutes on this phone)"
                                % os.path.basename(model["path"]))
            err = self.start(model)
            if err:
                yield event("switch_error", message=err)
                return

            t0 = time.time()
            last = t0
            while True:
                if self.healthy():
                    self.current = mid
                    self.loaded_seconds = round(time.time() - t0, 1)
                    yield event("switch_ready", model=mid, label=model["label"],
                                seconds=self.loaded_seconds)
                    return
                if self.proc is not None and self.proc.poll() is not None:
                    yield event("switch_error",
                                message="model server exited during load (code %s)\n%s"
                                        % (self.proc.returncode, self.log_tail()))
                    return
                if time.time() - t0 > self.cfg.load_timeout:
                    yield event("switch_error",
                                message="timed out after %ds waiting for the model to load\n%s"
                                        % (self.cfg.load_timeout, self.log_tail()))
                    return
                if time.time() - last >= 10:
                    last = time.time()
                    yield event("switch_phase", phase="loading",
                                elapsed=round(time.time() - t0, 1),
                                message="still loading (%ds)" % int(time.time() - t0))
                time.sleep(3)
        finally:
            with self.lock:
                self.switching = False


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    cfg = None
    manager = None
    server_version = "BonsaiHarness/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default
        log("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_send(self, ev):
        self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, json.dumps({"error": "index.html missing: %s" % exc}))
            return
        if path == "/api/info":
            self._send(200, json.dumps(self._info()))
            return
        if path == "/api/models":
            self._send(200, json.dumps(self.manager.models_payload()))
            return
        if path == "/favicon.ico":
            self._send(204, b"")
            return
        self._send(404, json.dumps({"error": "not found"}))

    def _info(self):
        cfg = self.cfg
        mgr = self.manager
        info = {
            "ok": False,
            "base_url": cfg.base_url,
            "workspace": cfg.workspace,
            "tools": TOOL_NAMES if cfg.allow_bash else [t for t in TOOL_NAMES if t != "run_bash"],
            "model": None,
            "model_id": mgr.current,
            "model_label": None,
            "n_ctx": None,
            "error": None,
        }
        try:
            with urllib.request.urlopen(cfg.base_url.rstrip("/") + "/props", timeout=10) as resp:
                props = json.loads(resp.read().decode("utf-8", "replace"))
            info["ok"] = True
            info["model"] = (props.get("model_path") or "").split("/")[-1] or None
            info["n_ctx"] = props.get("default_generation_settings", {}).get("n_ctx")
        except Exception as exc:  # noqa: BLE001
            info["error"] = "model server unreachable: %s" % exc
        m = mgr.model(mgr.current) if mgr.current else None
        info["model_label"] = m["label"] if m else info["model"]
        return info

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/run", "/api/switch"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, json.dumps({"error": "bad request body: %s" % exc}))
            return

        if path == "/api/switch":
            self._handle_switch(body)
            return
        self._handle_run(body)

    def _handle_switch(self, body):
        mid = str(body.get("model") or "").strip()
        if not mid:
            self._send(400, json.dumps({"error": "model is required"}))
            return
        self._sse_start()
        try:
            for ev in self.manager.switch_events(mid):
                self._sse_send(ev)
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected during model switch (switch continues)")
        except Exception as exc:  # noqa: BLE001
            log("switch failed: %s" % exc)
            try:
                self._sse_send(event("switch_error", message=str(exc)))
            except Exception:  # noqa: BLE001
                pass

    def _handle_run(self, body):
        task = (body.get("task") or "").strip()
        if not task:
            self._send(400, json.dumps({"error": "task is required"}))
            return
        try:
            max_steps = max(1, min(20, int(body.get("max_steps") or self.cfg.max_steps)))
            max_tokens = max(16, min(2048, int(body.get("max_tokens") or self.cfg.max_tokens)))
        except (TypeError, ValueError):
            self._send(400, json.dumps({"error": "max_steps / max_tokens must be integers"}))
            return

        blocked = self.manager.begin_turn()
        if blocked:
            self._send(409, json.dumps({"error": blocked}))
            return

        try:
            self._sse_start()
            for ev in run_agent(self.cfg, task, max_steps, max_tokens):
                self._sse_send(ev)
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected; run abandoned")
        except Exception as exc:  # noqa: BLE001
            log("run failed: %s" % exc)
            try:
                self._sse_send(event("error", message=str(exc)))
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.manager.end_turn()


def parse_models(spec):
    if not spec:
        raw = DEFAULT_MODELS
    else:
        raw = json.loads(spec)
    models = []
    for item in raw:
        if not isinstance(item, dict) or "path" not in item:
            raise SystemExit("each model needs at least {\"path\": ...}")
        path = os.path.realpath(os.path.expanduser(item["path"]))
        models.append({
            "id": str(item.get("id") or os.path.basename(path)),
            "label": str(item.get("label") or os.path.basename(path)),
            "path": path,
        })
    if not models:
        raise SystemExit("no models configured")
    return models


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agentic harness for a local llama-server.")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0, so LAN browsers can reach it)")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080", help="OpenAI-compatible model server")
    ap.add_argument("--workspace", default=os.path.expanduser("~/agent-workspace"),
                    help="directory the tools are confined to")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--timeout", type=int, default=7200, help="per model request timeout, seconds")
    ap.add_argument("--no-bash", action="store_true", help="disable the run_bash tool")
    ap.add_argument("--models", default=None,
                    help="JSON list of {\"id\",\"label\",\"path\"} to offer in the UI")
    ap.add_argument("--server-script", default=DEFAULT_SERVER_SCRIPT,
                    help="start script used to (re)load a model")
    ap.add_argument("--server-log", default=DEFAULT_SERVER_LOG,
                    help="log file the model server writes to")
    ap.add_argument("--load-timeout", type=int, default=1800,
                    help="seconds to wait for a freshly loaded model to answer /health")
    ap.add_argument("--unload-timeout", type=int, default=60,
                    help="seconds to wait for the old model server to exit")
    cfg = ap.parse_args(argv)

    cfg.workspace = os.path.realpath(os.path.expanduser(cfg.workspace))
    os.makedirs(cfg.workspace, exist_ok=True)
    cfg.allow_bash = not cfg.no_bash
    cfg.models = parse_models(cfg.models)
    cfg.server_script = os.path.realpath(os.path.expanduser(cfg.server_script))
    cfg.server_log = os.path.realpath(os.path.expanduser(cfg.server_log))
    cfg.base_url = cfg.base_url.rstrip("/")

    manager = ModelManager(cfg)
    manager.detect_current()

    Handler.cfg = cfg
    Handler.manager = manager
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True
    log("harness on http://%s:%d  (model %s, workspace %s, loaded %s)"
        % (cfg.host, cfg.port, cfg.base_url, cfg.workspace, manager.current))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
