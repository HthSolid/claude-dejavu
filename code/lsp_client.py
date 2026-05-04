#!/usr/bin/env python3
"""
LSP delegation — pipes proposed edits through tsserver / pyright / etc.
for ground-truth verification.

v0.3.9: Full JSON-RPC stdio implementation. v0.3.2.1 scaffold replaced.

Architecture:

  - One persistent client per (language, project_root) pair, lazily
    spawned the first time a verification is requested. Stdio JSON-RPC
    stays open across calls — cold spawn ~1-2 s, warm verify ~50 ms.
  - Standard LSP base protocol: Content-Length-prefixed JSON-RPC 2.0
    messages over stdin/stdout.
  - Lifecycle:
      initialize → initialized → didOpen → didChange → wait for
      publishDiagnostics → didClose
  - On any failure (binary missing, hang, JSON-RPC error, timeout) we
    fall back silently to the no-LSP path. The lint never blocks on LSP.

Server selection:
  typescript-language-server is preferred (official LSP wrapper around
  tsserver). For Python: pyright-langserver, then jedi-language-server.

Cache key:
  (language, project_root) — multiple project roots run multiple
  servers, but the same project re-uses the warm one.

Process safety:
  - SIGTERM the server cleanly when the Python process exits (atexit).
  - Reader thread per client to drain stdout asynchronously (LSP servers
    send unsolicited notifications like publishDiagnostics).
  - Stderr drained to /dev/null (or a per-client log file in debug mode).
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


# ─── Detection ─────────────────────────────────────────────────────────────


_LSP_BINARIES = {
    "typescript": ["typescript-language-server"],
    "tsx": ["typescript-language-server"],
    "javascript": ["typescript-language-server"],
    "python": ["pyright-langserver", "jedi-language-server", "pylsp"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
}


def find_lsp_binary(language: str) -> str | None:
    """Return the first LSP binary on PATH for this language, or None.

    Honors `CLAUDE_DEJAVU_LSP_BINARY_<lang>` env vars so tests + advanced
    users can override the auto-detected binary (or point it at a mock
    server). Example: CLAUDE_DEJAVU_LSP_BINARY_typescript=/path/to/mock.
    """
    override = os.environ.get(f"CLAUDE_DEJAVU_LSP_BINARY_{language}")
    if override and Path(override).exists():
        return override
    candidates = _LSP_BINARIES.get(language, [])
    for cmd in candidates:
        path = shutil.which(cmd)
        if path:
            return path
    return None


def is_lsp_available(language: str) -> bool:
    return find_lsp_binary(language) is not None


def list_available_lsps() -> dict[str, str | None]:
    return {lang: find_lsp_binary(lang) for lang in _LSP_BINARIES}


# ─── LSP wire protocol ─────────────────────────────────────────────────────


_LANGUAGE_ID = {
    "typescript": "typescript",
    "tsx": "typescriptreact",
    "javascript": "javascript",
    "jsx": "javascriptreact",
    "python": "python",
    "rust": "rust",
    "go": "go",
}


def _server_args(binary: str) -> list[str]:
    """Return the args to spawn the LSP server in stdio mode."""
    name = os.path.basename(binary)
    # Python scripts (used by mock servers in tests) need an interpreter.
    if binary.endswith(".py"):
        return [sys.executable, binary]
    if name in ("typescript-language-server",):
        return [binary, "--stdio"]
    if name in ("pyright-langserver",):
        return [binary, "--stdio"]
    if name in ("jedi-language-server", "pylsp", "rust-analyzer", "gopls"):
        # All of these default to stdio when invoked with no args.
        return [binary]
    return [binary, "--stdio"]


class LspClient:
    """One persistent LSP server connection for one (language, root) pair.

    Thread model: the constructor spawns the subprocess + a single reader
    thread that drains stdout. The reader puts every parsed message on
    `_inbox` (a Queue). The main thread sends requests + collects
    responses by id.

    This class is intentionally minimal — just enough to: initialize,
    open a doc, send didChange, collect publishDiagnostics, and close.
    """

    def __init__(self, binary: str, language: str, root_uri: str):
        self.binary = binary
        self.language = language
        self.root_uri = root_uri
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        # publishDiagnostics is keyed by document URI.
        self._diagnostics: dict[str, list[dict]] = {}
        # Track in-flight request ids → Future-like result holders.
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._initialized = False
        self._dead_reason: str | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    def start(self, init_timeout: float = 8.0) -> bool:
        """Spawn the subprocess + complete the LSP `initialize` handshake.
        Returns True on success, False on any failure (binary missing,
        timeout, JSON-RPC error). Failure is non-fatal — the caller
        falls back to the no-LSP path."""
        try:
            self._proc = subprocess.Popen(
                _server_args(self.binary),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,  # unbuffered — LSP framing is binary
            )
        except OSError as e:
            self._dead_reason = f"spawn failed: {e}"
            return False

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True,
        )
        self._reader_thread.start()
        atexit.register(self.shutdown)

        # initialize handshake.
        init_id = self._send_request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "claude-dejavu", "version": "0.3.9"},
            "rootUri": self.root_uri,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {
                        "relatedInformation": False,
                        "tagSupport": {"valueSet": [1, 2]},
                    },
                    "synchronization": {
                        "didSave": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                    },
                },
                "workspace": {
                    "workspaceFolders": True,
                },
            },
            "workspaceFolders": [{"uri": self.root_uri, "name": "project"}],
            "trace": "off",
        })
        try:
            resp = self._await_response(init_id, timeout=init_timeout)
        except TimeoutError:
            self._dead_reason = "initialize timeout"
            self.shutdown()
            return False
        if resp.get("error"):
            self._dead_reason = f"initialize error: {resp['error']}"
            self.shutdown()
            return False

        # Notify initialized (no response expected).
        self._send_notification("initialized", {})
        self._initialized = True
        return True

    def shutdown(self):
        if self._proc is None:
            return
        try:
            # Best effort — many servers ignore shutdown errors.
            self._send_request("shutdown", None, await_response=False)
            self._send_notification("exit", None)
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    # ─── Document operations ──────────────────────────────────────────

    def verify(self, file_path: str, proposed_content: str,
                wait_s: float = 2.0) -> list[dict]:
        """Open `file_path` with `proposed_content`, wait up to wait_s
        for diagnostics to settle, then close. Returns the list of LSP
        diagnostics for this document.

        Diagnostics shape (LSP-native):
          {
            "range": {"start": {"line": int, "character": int}, "end": ...},
            "severity": 1 | 2 | 3 | 4,    # error | warning | info | hint
            "code": str | int | None,
            "message": str,
            "source": str | None,
          }
        """
        if not self._initialized or self._proc is None:
            return []
        uri = Path(file_path).resolve().as_uri()
        # Reset diagnostics for this URI before opening.
        with self._lock:
            self._diagnostics[uri] = []
        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": _LANGUAGE_ID.get(self.language, self.language),
                "version": 1,
                "text": proposed_content,
            },
        })
        # Some servers don't emit diagnostics on didOpen alone; nudging
        # them with a didChange (text identical, version bump) helps.
        self._send_notification("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": proposed_content}],
        })
        # Wait for diagnostics to settle. There's no LSP "I'm done"
        # signal — we wait for a quiet period after the last
        # publishDiagnostics. Strategy: poll every 100 ms; if no new
        # diagnostics in 250 ms after we've seen at least one, return.
        deadline = time.monotonic() + wait_s
        last_seen_count = -1
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            with self._lock:
                cur_count = len(self._diagnostics.get(uri, []))
            if cur_count != last_seen_count:
                last_seen_count = cur_count
                quiet_since = time.monotonic()
            elif quiet_since is not None and time.monotonic() - quiet_since > 0.25:
                break
            time.sleep(0.05)
        with self._lock:
            diags = list(self._diagnostics.get(uri, []))
        # Close the doc so subsequent verifications start clean.
        try:
            self._send_notification("textDocument/didClose", {
                "textDocument": {"uri": uri},
            })
        except Exception:
            pass
        return diags

    # ─── Wire protocol ────────────────────────────────────────────────

    def _send_request(self, method: str, params: Any,
                       await_response: bool = True) -> str:
        rid = str(uuid.uuid4())
        if await_response:
            self._pending[rid] = queue.Queue()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write_msg(msg)
        return rid

    def _send_notification(self, method: str, params: Any):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write_msg(msg)

    def _await_response(self, rid: str, timeout: float = 5.0) -> dict:
        q = self._pending.get(rid)
        if q is None:
            raise RuntimeError(f"no pending request for id={rid}")
        try:
            return q.get(timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    def _write_msg(self, msg: dict):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("LSP process not running")
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self._proc.stdin.write(header)
            self._proc.stdin.write(body)
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            self._dead_reason = f"write failed: {e}"
            raise

    def _reader_loop(self):
        """Continuously read framed JSON-RPC messages from the server's
        stdout. Dispatch responses to pending requests, accumulate
        publishDiagnostics into _diagnostics."""
        if self._proc is None or self._proc.stdout is None:
            return
        stream = self._proc.stdout
        while True:
            try:
                # Read headers until \r\n\r\n.
                headers = b""
                while True:
                    chunk = stream.read(1)
                    if not chunk:
                        return
                    headers += chunk
                    if headers.endswith(b"\r\n\r\n"):
                        break
                    if len(headers) > 8192:
                        # Sanity bound — abort parser if header overflows.
                        return
                content_length = 0
                for line in headers.decode("ascii", errors="ignore").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            content_length = 0
                if content_length <= 0:
                    continue
                body = b""
                remaining = content_length
                while remaining > 0:
                    chunk = stream.read(remaining)
                    if not chunk:
                        return
                    body += chunk
                    remaining -= len(chunk)
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
            except Exception as e:
                self._dead_reason = f"reader crashed: {e}"
                return

    def _dispatch(self, msg: dict):
        # Response to a request we sent?
        if "id" in msg and ("result" in msg or "error" in msg):
            q = self._pending.get(msg["id"])
            if q is not None:
                q.put(msg)
            return
        # Notification from the server.
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri")
            diags = params.get("diagnostics", [])
            if uri:
                with self._lock:
                    self._diagnostics[uri] = diags
            return
        if method == "window/logMessage" or method == "window/showMessage":
            # Drop — we don't surface these.
            return
        # Other notifications (workspace/configuration, etc.) — ignore;
        # for a verify-only client we don't need to respond.

    @property
    def alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and self._initialized)


# ─── Client pool ───────────────────────────────────────────────────────────


_pool: dict[tuple[str, str], LspClient] = {}
_pool_lock = threading.Lock()


def _get_or_spawn(language: str, project_root: str | None,
                   binary: str) -> LspClient | None:
    root = str(Path(project_root or os.getcwd()).resolve())
    root_uri = Path(root).as_uri()
    key = (language, root)
    with _pool_lock:
        client = _pool.get(key)
        if client is not None and client.alive:
            return client
        client = LspClient(binary, language, root_uri)
        if not client.start():
            return None
        _pool[key] = client
        return client


# ─── Public API ────────────────────────────────────────────────────────────


def verify(file_path: str, proposed_content: str, language: str,
            project_root: str | None = None,
            timeout_s: float = 4.0) -> dict:
    """Verify proposed file content via LSP. Returns the same shape the
    v0.3.2.1 scaffold returned, but now diagnostics are real.
    """
    if os.environ.get("CLAUDE_DEJAVU_LSP_DISABLE"):
        return {
            "available": False, "binary": None, "diagnostics": [],
            "skipped_reason": "disabled via CLAUDE_DEJAVU_LSP_DISABLE",
        }
    binary = find_lsp_binary(language)
    if binary is None:
        return {
            "available": False, "binary": None, "diagnostics": [],
            "skipped_reason": (f"no LSP binary on PATH for language='{language}'. "
                               f"Install one of {_LSP_BINARIES.get(language, [])}."),
        }
    client = _get_or_spawn(language, project_root, binary)
    if client is None:
        return {
            "available": False, "binary": binary, "diagnostics": [],
            "skipped_reason": "LSP server failed to initialize",
        }
    try:
        raw_diags = client.verify(file_path, proposed_content,
                                    wait_s=timeout_s)
    except Exception as e:
        return {
            "available": False, "binary": binary, "diagnostics": [],
            "skipped_reason": f"LSP verify raised: {type(e).__name__}: {e}",
        }
    # Translate LSP severity ints → strings.
    sev_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    out = []
    src = Path(file_path).resolve()
    src_lines = proposed_content.splitlines()
    for d in raw_diags:
        rng = d.get("range") or {}
        start = rng.get("start") or {}
        line = start.get("line") or 0
        col = start.get("character") or 0
        # Best-effort name extraction: pull the identifier under the
        # diagnostic's start position so the strategy can attribute the
        # issue to a specific name.
        name_referenced = None
        try:
            ln = src_lines[line]
            tail = ln[col:]
            import re as _re
            m = _re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", tail)
            if m:
                name_referenced = m.group(0)
        except Exception:
            pass
        out.append({
            "severity": sev_map.get(d.get("severity", 2), "warning"),
            "message": d.get("message", "").strip(),
            "code": d.get("code"),
            "source": d.get("source"),
            "line": line + 1,        # report 1-indexed
            "column": col + 1,
            "name_referenced": name_referenced,
        })
    return {
        "available": True, "binary": binary,
        "diagnostics": out,
        "skipped_reason": None,
    }


def shutdown_all():
    """Tear down every pooled LSP client. Called from atexit and from
    `claude-dejavu doctor` after a one-shot health probe."""
    with _pool_lock:
        for client in _pool.values():
            try:
                client.shutdown()
            except Exception:
                pass
        _pool.clear()


atexit.register(shutdown_all)
