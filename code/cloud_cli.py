#!/usr/bin/env python3
"""
Cloud-tier CLI flows: login, logout, status.

`login` runs an OAuth-style device flow:

    plugin                     browser                license-server
    ──────                     ───────                ─────────
    1. start localhost:N + state token
    2. open https://license.hte.digital/auth?for=dejavu&port=N&state=<...>
                                ─────►  3. user authenticates (existing the license server)
                                        4. server checks active Pro sub
                                        5. server calls Worker /admin/keys → key
                                        6. server redirects to http://localhost:N/?key=<...>&state=<...>
    7. callback receives key, validates state, writes to ~/.config/claude-dejavu/cloud.env
    8. CLI verifies key via Worker /v1/ping + tiny embed
    9. CLI flips DEJAVU_EMBED_MODE=cloud in config.env

`login --paste` skips the browser flow — the user pastes a key minted via
some other means (e.g. an admin-issued onboarding key). Useful for HTE
internal dogfood + as a fallback when the OAuth landing page is down.

`logout` deletes the local key file and asks the license server to revoke server-side.

`status --cloud` shows: current mode, key prefix, worker reachability,
quota used / remaining, last error.
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

DEFAULT_LICENSE_BASE = "https://license.hte.digital"
DEFAULT_CLOUD_URL = "https://embed.hte.digital"
DEFAULT_CALLBACK_TIMEOUT_SEC = 300  # 5 minutes


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or
                Path.home() / ".config") / "claude-dejavu"


def _cloud_env_file() -> Path:
    return _config_dir() / "cloud.env"


def _config_env_file() -> Path:
    """The plugin's primary config.env, normally written by install.py.
    We APPEND cloud-tier values here so ingester.py picks them up."""
    data_root = Path(os.environ.get("CLAUDE_DEJAVU_DATA_ROOT") or
                     Path(os.environ.get("XDG_DATA_HOME") or
                          Path.home() / ".local/share") / "claude-dejavu")
    return data_root / "config.env"


def _load_config_env() -> dict[str, str]:
    """Parse simple KEY=VALUE config.env, ignoring comments + blanks."""
    out: dict[str, str] = {}
    p = _config_env_file()
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _set_config_env(kv: dict[str, str]) -> None:
    """Set or update keys in config.env, appending if new, replacing if existing.
    Preserves comments + ordering of unrelated lines."""
    p = _config_env_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        if "=" not in stripped:
            out_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in kv:
            out_lines.append(f"{k}={kv[k]}")
            seen.add(k)
        else:
            out_lines.append(line)
    for k, v in kv.items():
        if k not in seen:
            out_lines.append(f"{k}={v}")
    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _backup_config_env() -> Optional[Path]:
    """Return a timestamped copy path so we can roll back if anything fails."""
    src = _config_env_file()
    if not src.exists():
        return None
    dst = src.with_suffix(f".bak.{int(time.time())}")
    dst.write_bytes(src.read_bytes())
    return dst


# ──────────────────────────────────────────────────────────── helpers


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, dict, bytes]:
    """Plain GET — returns (status, headers, body) with no exception leak."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:  # noqa: BLE001
        return 0, {"x-error": f"{type(e).__name__}: {e}"}, b""


def _http_post(url: str, body: dict, headers: Optional[dict] = None,
               timeout: float = 30.0) -> tuple[int, dict, bytes]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:  # noqa: BLE001
        return 0, {"x-error": f"{type(e).__name__}: {e}"}, b""


def _free_port() -> int:
    """Bind 0 and let the OS pick. Close immediately — caller will rebind."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cloud_url() -> str:
    cfg = _load_config_env()
    return (os.environ.get("DEJAVU_CLOUD_URL")
            or cfg.get("DEJAVU_CLOUD_URL")
            or DEFAULT_CLOUD_URL).rstrip("/")


def _license_base() -> str:
    cfg = _load_config_env()
    return (os.environ.get("DEJAVU_LICENSE_URL")
            or cfg.get("DEJAVU_LICENSE_URL")
            or DEFAULT_LICENSE_BASE).rstrip("/")


def _verify_key(key: str) -> tuple[bool, str]:
    """Hit /v1/whoami to confirm the key works. Costs ZERO cloud tokens
    (earlier versions called /v1/embed which charged ~10 tokens per
    verify, surprising users on tight quotas). Returns (ok, detail).
    """
    if not key.startswith(("dvk_live_", "dvk_test_")):
        return False, "key has wrong format (expected dvk_live_… or dvk_test_…)"
    url = _cloud_url()
    status, _headers, body = _http_get(
        f"{url}/v1/whoami",
        timeout=5,
    )
    # _http_get doesn't take headers — make the call manually
    req = urllib.request.Request(
        f"{url}/v1/whoami", method="GET",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        remaining = data.get("quota_remaining", "?")
        return True, f"ok (quota_remaining={remaining})"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "key rejected (401)"
        if e.code == 402:
            return False, "monthly quota exhausted (402)"
        detail = e.read()[:160].decode("utf-8", errors="replace")
        return False, f"verify failed (HTTP {e.code}): {detail}"
    except Exception as e:  # noqa: BLE001
        return False, f"verify failed: {type(e).__name__}: {e}"


# ──────────────────────────────────────────────────────────── login


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP server. Stores the received key on the server instance,
    sends a friendly success page, then signals the main thread to shutdown."""

    server_version = "DejavuCallback/1"
    received_key: Optional[str] = None
    received_state: Optional[str] = None
    received_error: Optional[str] = None

    def log_message(self, fmt, *args):  # silence stderr access logging
        return

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # Mark the result on the server so the waiting thread can read it.
        srv = self.server
        srv.received_key = (params.get("key") or [None])[0]  # type: ignore[attr-defined]
        srv.received_state = (params.get("state") or [None])[0]  # type: ignore[attr-defined]
        srv.received_error = (params.get("error") or [None])[0]  # type: ignore[attr-defined]

        if srv.received_error:  # type: ignore[attr-defined]
            page = (b"<!doctype html><html><body>"
                    b"<h1>claude-dejavu login: failed</h1>"
                    b"<p>You can close this tab and check the terminal.</p>"
                    b"</body></html>")
        else:
            page = (b"<!doctype html><html><body>"
                    b"<h1>claude-dejavu: signed in</h1>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()


def _wait_for_key(port: int, expected_state: str, timeout: float
                  ) -> tuple[Optional[str], Optional[str]]:
    """Spin up the callback server and block until it receives the key.
    Returns (key, error)."""
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.received_key = None  # type: ignore[attr-defined]
    server.received_state = None  # type: ignore[attr-defined]
    server.received_error = None  # type: ignore[attr-defined]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    started = time.monotonic()
    try:
        while time.monotonic() - started < timeout:
            if server.received_key or server.received_error:  # type: ignore[attr-defined]
                # Give the response a moment to flush before shutdown.
                time.sleep(0.2)
                break
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    state = server.received_state  # type: ignore[attr-defined]
    if state and state != expected_state:
        return None, "state mismatch (possible CSRF — login aborted)"
    err = server.received_error  # type: ignore[attr-defined]
    if err:
        return None, err
    key = server.received_key  # type: ignore[attr-defined]
    if not key:
        return None, "timed out waiting for browser callback"
    return key, None


def cmd_login(args) -> int:
    if getattr(args, "paste", False):
        return _login_paste(args)
    return _login_browser(args)


def _login_paste(args) -> int:
    print("Paste your dejavu cloud key (dvk_live_… or dvk_test_…):")
    # Prefer getpass when stdin is a TTY (hides input from screen + history).
    # Otherwise fall through to plain input() — non-TTY pipes can't disable
    # terminal echo, and getpass falls back with a noisy "Can not control
    # echo on the terminal" warning that confuses scripted users.
    if sys.stdin.isatty():
        key = getpass.getpass(prompt="key: ").strip()
    else:
        sys.stderr.write("  (non-TTY stdin: input will be echoed)\n")
        key = (sys.stdin.readline() or "").strip()
    if not key:
        print("  no key provided — aborting", file=sys.stderr)
        return 1
    return _finalize_login(key)


def _login_browser(args) -> int:
    license_base = getattr(args, "license_url", None) or _license_base()
    callback_port = _free_port()
    state = secrets.token_urlsafe(24)
    callback_url = f"http://127.0.0.1:{callback_port}/"
    auth_url = (
        f"{license_base}/dejavu/auth"
        f"?port={callback_port}"
        f"&state={urllib.parse.quote(state)}"
        f"&callback={urllib.parse.quote(callback_url)}"
    )
    print(f"opening browser to {license_base}/dejavu/auth …")
    print(f"if it does not open, visit:\n  {auth_url}")
    print(f"waiting up to {DEFAULT_CALLBACK_TIMEOUT_SEC}s for callback…")
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass

    key, err = _wait_for_key(callback_port, expected_state=state,
                             timeout=DEFAULT_CALLBACK_TIMEOUT_SEC)
    if err:
        print(f"  login failed: {err}", file=sys.stderr)
        print("  if the browser flow is unavailable, "
              "try:  claude-dejavu login --paste", file=sys.stderr)
        return 1
    assert key is not None
    return _finalize_login(key)


def _finalize_login(key: str) -> int:
    print(f"verifying key against {_cloud_url()}…")
    ok, detail = _verify_key(key)
    if not ok:
        print(f"  {detail}", file=sys.stderr)
        return 1
    print(f"  {detail}")
    backup = _backup_config_env()
    if backup:
        print(f"  config.env backed up at {backup.name}")
    _set_config_env({
        "DEJAVU_EMBED_MODE": "cloud",
        "DEJAVU_CLOUD_KEY": key,
        "DEJAVU_CLOUD_URL": _cloud_url(),
    })
    cfg_path = _config_env_file()
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass
    print(f"  wrote {cfg_path}")
    print("  cloud mode is now active. Future ingest will route through the managed embedder.")
    print("  privacy reminder: cloud mode sends turn text to a managed embedder.")
    print("                    The upstream provider does not log content by default")
    print("                    and never trains on your data, but their TOS allows brief")
    print("                    debug logging. Use `claude-dejavu logout` to switch back")
    print("                    to local mode any time.")
    return 0


# ──────────────────────────────────────────────────────────── logout


def cmd_logout(args) -> int:
    cfg = _load_config_env()
    key = cfg.get("DEJAVU_CLOUD_KEY", "").strip()
    license_base = getattr(args, "license_url", None) or _license_base()

    if not key:
        print("no cloud key on this machine — nothing to revoke locally")
    else:
        prefix = key[:16]
        if not getattr(args, "skip_revoke", False):
            print(f"requesting server-side revoke of {prefix}…")
            url = f"{license_base}/api/v1/dejavu/revoke"
            status, _, body = _http_post(url, {"prefix": prefix}, timeout=15)
            if status == 200:
                print("  revoked")
            elif status == 0:
                print(f"  could not reach license server — local key removed only")
            else:
                detail = body[:160].decode("utf-8", errors="replace")
                print(f"  revoke returned HTTP {status}: {detail}", file=sys.stderr)
                print("  proceeding with local removal anyway")

    backup = _backup_config_env()
    if backup:
        print(f"  config.env backed up at {backup.name}")
    _set_config_env({
        "DEJAVU_EMBED_MODE": "local",
        "DEJAVU_CLOUD_KEY": "",
    })
    print("  switched to local mode")
    return 0


# ──────────────────────────────────────────────────────────── status


def cmd_status(args) -> int:
    cfg = _load_config_env()
    mode = (os.environ.get("DEJAVU_EMBED_MODE")
            or cfg.get("DEJAVU_EMBED_MODE", "local")).strip().lower()
    cloud_url = _cloud_url()
    key = (os.environ.get("DEJAVU_CLOUD_KEY")
           or cfg.get("DEJAVU_CLOUD_KEY", "")).strip()
    prefix = key[:16] if key else "(none)"

    print(f"claude-dejavu cloud status")
    print(f"  mode:       {mode}")
    print(f"  worker:     {cloud_url}")
    print(f"  key prefix: {prefix}")

    # ping
    status, _, body = _http_get(f"{cloud_url}/v1/ping", timeout=5)
    if status == 200:
        try:
            data = json.loads(body)
            print(f"  worker:     ok (v{data.get('version')}, "
                  f"model={data.get('model')})")
            b = data.get("batching") or {}
            if b:
                print(f"  batching:   current_batch={b.get('current_batch')} "
                      f"p50={b.get('p50_ms')}ms p99={b.get('p99_ms')}ms")
        except Exception:  # noqa: BLE001
            print(f"  worker:     ok (HTTP 200, body unparseable)")
    else:
        print(f"  worker:     unreachable (HTTP {status})")
        return 1

    if mode != "cloud":
        print(f"  ⓘ run `claude-dejavu login` to switch to cloud mode")
        return 0

    if not key:
        print(f"  ⚠ cloud mode set but no key — run `claude-dejavu login`")
        return 1

    # tiny embed roundtrip — same as doctor probe
    ok, detail = _verify_key(key)
    if ok:
        print(f"  embed:      {detail}")
    else:
        print(f"  embed:      FAIL — {detail}")
        return 1
    return 0
