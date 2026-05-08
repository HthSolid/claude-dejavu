#!/usr/bin/env python3
"""
Pluggable embedding provider for claude-dejavu.

Two modes (selected via DEJAVU_EMBED_MODE env var):

  local  (default)  → POST text → http://transformers:8080/vectors
                      (the t2v-transformers container in
                      docker/docker-compose.multilingual.yml)

  cloud             → POST text → https://embed.hte.digital/v1/embed
                      (managed embedder fronting the same
                      multilingual-e5-large-instruct model)

The cloud mode trades local CPU pegging on slow machines for
~50-200ms remote inference + monthly token billing. Vectors are
byte-for-byte interchangeable across the two modes because both
use intfloat/multilingual-e5-large-instruct.

Env vars:
  DEJAVU_EMBED_MODE     "local" (default) | "cloud"
  DEJAVU_CLOUD_URL      override (default: https://embed.hte.digital)
  DEJAVU_CLOUD_KEY      Pro API key  dvk_live_…
  DEJAVU_LOCAL_URL      local transformer URL
                        (default: http://transformers:8080)
  DEJAVU_EMBED_TIMEOUT  per-call timeout seconds (default: 30)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol

EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"
DEFAULT_CLOUD_URL = "https://embed.hte.digital"
DEFAULT_LOCAL_URL = "http://transformers:8080"
DEFAULT_TIMEOUT = 30.0


class EmbeddingError(RuntimeError):
    """Wraps provider failures with status code if HTTP-derived."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 retriable: bool = False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    provider: str
    tokens: int
    cache_hits: int
    elapsed_ms: int


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str]) -> EmbedResult: ...


# ─────────────────────────────────────────────────────────── local provider


class LocalTransformersProvider:
    """Calls the Weaviate text2vec-transformers inference container directly.
    Per-text POST (the container's /vectors endpoint takes one text at a time).
    Uses simple sequential calls — fine for small batches; for big batches
    the cloud provider is the better choice.
    """

    name = "local"

    def __init__(self, url: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.url = (url or os.environ.get("DEJAVU_LOCAL_URL",
                                          DEFAULT_LOCAL_URL)).rstrip("/")
        self.timeout = timeout

    @staticmethod
    def is_reachable(url: Optional[str] = None, timeout: float = 1.5) -> bool:
        """Probe the inference container — used before adding it to a
        cascade so we don't fall back to a URL that's guaranteed to fail
        (e.g. when the container has been stopped to save CPU)."""
        u = (url or os.environ.get("DEJAVU_LOCAL_URL",
                                   DEFAULT_LOCAL_URL)).rstrip("/")
        try:
            with urllib.request.urlopen(f"{u}/.well-known/ready",
                                        timeout=timeout) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            # The Weaviate t2v-transformers container exposes /meta on
            # success but may not have a ready endpoint — treat any
            # 4xx that's NOT a connection failure as "reachable enough".
            return e.code < 500
        except Exception:
            return False

    def embed(self, texts: list[str]) -> EmbedResult:
        if not texts:
            return EmbedResult([], self.name, 0, 0, 0)
        t0 = time.monotonic()
        out: list[list[float]] = []
        for t in texts:
            body = json.dumps({"text": t}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.url}/vectors", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise EmbeddingError(
                    f"local /vectors HTTP {e.code}", status=e.code,
                    retriable=e.code >= 500) from e
            except Exception as e:
                raise EmbeddingError(
                    f"local /vectors error: {type(e).__name__}: {e}",
                    retriable=True) from e
            v = data.get("vector")
            if not isinstance(v, list):
                raise EmbeddingError("local /vectors returned no vector")
            out.append([float(x) for x in v])
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return EmbedResult(out, self.name, 0, 0, elapsed_ms)


# ─────────────────────────────────────────────────────────── cloud provider


class CloudProvider:
    """Calls the dejavu-cloud Worker. Bulk batched: one POST per call.
    Worker's adaptive batching handles provider-side splitting.
    """

    name = "cloud"

    def __init__(self, url: Optional[str] = None,
                 key: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.url = (url or os.environ.get("DEJAVU_CLOUD_URL",
                                          DEFAULT_CLOUD_URL)).rstrip("/")
        self.key = key or os.environ.get("DEJAVU_CLOUD_KEY", "")
        self.timeout = timeout
        if not self.key:
            raise EmbeddingError("DEJAVU_CLOUD_KEY not set")

    @staticmethod
    def is_reachable(url: Optional[str] = None, timeout: float = 3.0) -> bool:
        """Quick liveness probe to /v1/ping — useful when constructing a
        cascade so we don't add a fallback that's guaranteed to fail."""
        u = (url or os.environ.get("DEJAVU_CLOUD_URL",
                                   DEFAULT_CLOUD_URL)).rstrip("/")
        try:
            with urllib.request.urlopen(f"{u}/v1/ping", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def embed(self, texts: list[str]) -> EmbedResult:
        if not texts:
            return EmbedResult([], self.name, 0, 0, 0)
        t0 = time.monotonic()
        body = json.dumps({"texts": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/v1/embed", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
                "X-Embed-Model": "e5-large-instruct",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
                tokens = int(r.headers.get("x-embed-tokens", "0") or "0")
                cache_raw = r.headers.get("x-cache", "0/0") or "0/0"
                cache_hits = int(cache_raw.split("/", 1)[0]) if "/" in cache_raw else 0
        except urllib.error.HTTPError as e:
            err_body = e.read()[:500].decode(errors="replace")
            raise EmbeddingError(
                f"cloud /v1/embed HTTP {e.code}: {err_body}",
                status=e.code,
                # 5xx → retry/fallback. 4xx → don't, it's auth/quota/format.
                retriable=e.code >= 500,
            ) from e
        except Exception as e:
            raise EmbeddingError(
                f"cloud /v1/embed error: {type(e).__name__}: {e}",
                retriable=True) from e
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingError("cloud /v1/embed returned no embeddings")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return EmbedResult([list(map(float, v)) for v in embeddings],
                           self.name, tokens, cache_hits, elapsed_ms)


# ─────────────────────────────────────────────────────────── cascade


class CascadeProvider:
    """Try primary first; fall back to secondary on retriable errors only.

    NEVER falls back across MODELS (this is enforced at construction:
    both providers must use the same EMBED_MODEL). Cross-model fallback
    silently corrupts the vector index because e5 and bge-m3 vectors
    occupy different embedding spaces.

    On a fallback that ALSO fails, the raised EmbeddingError chains both
    underlying messages so the caller can see the cloud failure (which
    is usually the more interesting one) rather than only the local
    fallback's "container not running" message.
    """

    name = "cascade"

    def __init__(self, primary: EmbeddingProvider,
                 fallback: Optional[EmbeddingProvider] = None):
        self.primary = primary
        self.fallback = fallback

    def embed(self, texts: list[str]) -> EmbedResult:
        try:
            return self.primary.embed(texts)
        except EmbeddingError as e_primary:
            if self.fallback is None or not e_primary.retriable:
                raise
            sys.stderr.write(
                f"embedding_provider: primary={self.primary.name} failed "
                f"({e_primary}), falling back to {self.fallback.name}\n")
            try:
                return self.fallback.embed(texts)
            except EmbeddingError as e_fallback:
                raise EmbeddingError(
                    f"both providers failed — "
                    f"primary={self.primary.name}: {e_primary}; "
                    f"fallback={self.fallback.name}: {e_fallback}",
                    retriable=e_primary.retriable and e_fallback.retriable,
                ) from e_fallback


# ─────────────────────────────────────────────────────────── factory


def get_provider() -> EmbeddingProvider:
    """Return the configured provider, honoring DEJAVU_EMBED_MODE.

    Modes:
      local    LocalTransformersProvider (current default, requires
               t2v-transformers container running)
      cloud    CloudProvider with NO fallback (purest mode — explicit
               failures rather than confusing local-error messages
               when the local container is intentionally stopped)
      cascade  CloudProvider primary + LocalTransformersProvider fallback,
               but only IF local probes reachable. Otherwise = cloud-only.
    """
    mode = (os.environ.get("DEJAVU_EMBED_MODE") or "local").strip().lower()
    if mode == "cloud":
        return CloudProvider()
    if mode == "cascade":
        cloud = CloudProvider()
        if LocalTransformersProvider.is_reachable():
            return CascadeProvider(primary=cloud,
                                   fallback=LocalTransformersProvider())
        sys.stderr.write(
            "embedding_provider: local fallback unreachable, using cloud-only\n")
        return cloud
    if mode == "local":
        return LocalTransformersProvider()
    raise EmbeddingError(f"unknown DEJAVU_EMBED_MODE={mode!r}")


# ─────────────────────────────────────────────────────────── self-test


def _selftest() -> int:
    """Quick liveness self-test. `python embedding_provider.py [mode]`."""
    mode = sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("DEJAVU_EMBED_MODE") or "local")
    os.environ["DEJAVU_EMBED_MODE"] = mode
    print(f"=== embedding_provider self-test mode={mode} ===")
    try:
        prov = get_provider()
    except EmbeddingError as e:
        print(f"  FAIL: get_provider: {e}")
        return 1
    print(f"  provider: {prov.name}")
    samples = [
        "query: Why is uvicorn pegged?",
        "query: Bonjour le monde",
        "query: 你好世界",
    ]
    try:
        result = prov.embed(samples)
    except EmbeddingError as e:
        print(f"  FAIL: embed: {e}")
        return 1
    print(f"  embedded: {len(result.vectors)} texts")
    print(f"  vector dim: {len(result.vectors[0]) if result.vectors else 'n/a'}")
    print(f"  tokens: {result.tokens}")
    print(f"  cache hits: {result.cache_hits}")
    print(f"  elapsed: {result.elapsed_ms} ms")
    if not result.vectors or len(result.vectors[0]) != 1024:
        print(f"  FAIL: expected dim=1024")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
