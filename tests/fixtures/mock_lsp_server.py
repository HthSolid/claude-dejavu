#!/usr/bin/env python3
"""
Tiny mock LSP server for smoke-testing claude-dejavu's lsp_client.py
without depending on an external typescript-language-server install.

Behavior:
  - Speaks LSP base protocol (Content-Length-prefixed JSON-RPC over stdio).
  - Responds to `initialize` with capabilities + null serverInfo.
  - On `initialized` notification: ready.
  - On `textDocument/didOpen`: emit one `error` diagnostic for the
    identifier `FAKE_BAD_NAME` if it appears in the document text, plus
    one `warning` for `FAKE_WARN_NAME`. Otherwise emit nothing.
  - On `textDocument/didChange`: re-emit diagnostics for the new text.
  - On `shutdown` / `exit`: clean exit.

This is just enough for the smoke test to exercise:
  * spawn + initialize handshake
  * didOpen + diagnostic delivery
  * the publishDiagnostics → diagnostics-in-result path
  * graceful shutdown
"""
from __future__ import annotations

import json
import sys
import threading


def _read_message():
    """Read one Content-Length-framed JSON-RPC message from stdin."""
    headers = b""
    while True:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        headers += ch
        if headers.endswith(b"\r\n\r\n"):
            break
    content_length = 0
    for line in headers.decode("ascii", errors="ignore").split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
    body = b""
    remaining = content_length
    while remaining > 0:
        chunk = sys.stdin.buffer.read(remaining)
        if not chunk:
            return None
        body += chunk
        remaining -= len(chunk)
    return json.loads(body.decode("utf-8"))


def _write_message(msg: dict):
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _make_diagnostic(line: int, character: int, name: str,
                       severity: int, code: str, message: str):
    return {
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + len(name)},
        },
        "severity": severity,
        "code": code,
        "source": "mock_lsp_server",
        "message": message,
    }


def _publish_for_text(uri: str, text: str):
    diags = []
    for ln_idx, ln in enumerate(text.splitlines()):
        col = ln.find("FAKE_BAD_NAME")
        if col >= 0:
            diags.append(_make_diagnostic(
                ln_idx, col, "FAKE_BAD_NAME",
                severity=1, code="MOCK_E001",
                message="Cannot find name 'FAKE_BAD_NAME'.",
            ))
        col = ln.find("FAKE_WARN_NAME")
        if col >= 0:
            diags.append(_make_diagnostic(
                ln_idx, col, "FAKE_WARN_NAME",
                severity=2, code="MOCK_W001",
                message="'FAKE_WARN_NAME' is deprecated.",
            ))
    _write_message({
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": uri, "diagnostics": diags},
    })


def main():
    while True:
        try:
            msg = _read_message()
        except Exception:
            return
        if msg is None:
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")

        if method == "initialize":
            _write_message({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "capabilities": {
                        "textDocumentSync": {
                            "openClose": True, "change": 1,
                        },
                    },
                    "serverInfo": {"name": "mock_lsp_server", "version": "1.0"},
                },
            })
        elif method == "initialized":
            pass  # notification, no response
        elif method == "textDocument/didOpen":
            doc = params.get("textDocument") or {}
            _publish_for_text(doc.get("uri", ""), doc.get("text", ""))
        elif method == "textDocument/didChange":
            doc = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            text = changes[-1].get("text") if changes else ""
            _publish_for_text(doc.get("uri", ""), text)
        elif method == "textDocument/didClose":
            pass
        elif method == "shutdown":
            _write_message({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "exit":
            return
        else:
            # Unknown request: respond with empty result if it has an id.
            if rid is not None:
                _write_message({"jsonrpc": "2.0", "id": rid, "result": None})


if __name__ == "__main__":
    main()
