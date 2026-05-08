"""
Unit tests for code/embedding_provider.py.

These tests do NOT hit the network — `urllib.request.urlopen` is patched.
Run via `python -m pytest tests/test_embedding_provider.py -v`.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

# Ensure code/ is importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'code'))

import embedding_provider as ep  # noqa: E402


class FakeResponse:
    """Minimal stand-in for urllib's response context manager."""

    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _http_error(code: int, body: bytes = b'') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url='http://x', code=code, msg='boom',
        hdrs=None, fp=io.BytesIO(body),  # type: ignore
    )


class GetProviderTests(unittest.TestCase):
    def setUp(self):
        # Always start clean — clear cloud env so default mode is local
        for k in ('DEJAVU_EMBED_MODE', 'DEJAVU_CLOUD_KEY', 'DEJAVU_CLOUD_URL', 'DEJAVU_LOCAL_URL'):
            os.environ.pop(k, None)

    def test_default_mode_is_local(self):
        prov = ep.get_provider()
        self.assertEqual(prov.name, 'local')

    def test_cloud_mode_returns_cloud_provider(self):
        os.environ['DEJAVU_EMBED_MODE'] = 'cloud'
        os.environ['DEJAVU_CLOUD_KEY'] = 'dvk_live_xxx'
        prov = ep.get_provider()
        self.assertEqual(prov.name, 'cloud')

    def test_cloud_mode_without_key_raises(self):
        os.environ['DEJAVU_EMBED_MODE'] = 'cloud'
        with self.assertRaises(ep.EmbeddingError) as ctx:
            ep.get_provider()
        # Pin the message — a regression that caused a different
        # EmbeddingError to surface (e.g. unknown_mode) would still pass
        # a bare assertRaises but break the actual error UX
        self.assertIn('DEJAVU_CLOUD_KEY', str(ctx.exception))

    def test_unknown_mode_raises(self):
        os.environ['DEJAVU_EMBED_MODE'] = 'nonsense'
        with self.assertRaises(ep.EmbeddingError) as ctx:
            ep.get_provider()
        self.assertIn('unknown', str(ctx.exception).lower())
        self.assertIn('nonsense', str(ctx.exception))

    def test_cascade_mode_uses_cloud_only_when_local_unreachable(self):
        os.environ['DEJAVU_EMBED_MODE'] = 'cascade'
        os.environ['DEJAVU_CLOUD_KEY'] = 'dvk_live_xxx'
        with patch.object(ep.LocalTransformersProvider, 'is_reachable', return_value=False):
            prov = ep.get_provider()
            # Falls back to cloud-only — name 'cloud', NOT 'cascade'
            self.assertEqual(prov.name, 'cloud')

    def test_cascade_mode_wraps_when_local_reachable(self):
        os.environ['DEJAVU_EMBED_MODE'] = 'cascade'
        os.environ['DEJAVU_CLOUD_KEY'] = 'dvk_live_xxx'
        with patch.object(ep.LocalTransformersProvider, 'is_reachable', return_value=True):
            prov = ep.get_provider()
            self.assertEqual(prov.name, 'cascade')


class CloudProviderTests(unittest.TestCase):
    def setUp(self):
        os.environ['DEJAVU_CLOUD_KEY'] = 'dvk_live_test'
        os.environ['DEJAVU_CLOUD_URL'] = 'https://worker.test'

    def tearDown(self):
        os.environ.pop('DEJAVU_CLOUD_KEY', None)
        os.environ.pop('DEJAVU_CLOUD_URL', None)

    def test_embed_parses_response(self):
        resp = FakeResponse(
            json.dumps({'embeddings': [[0.1, 0.2, 0.3]]}).encode(),
            headers={'x-embed-tokens': '15', 'x-cache': '0/1'},
        )
        with patch('urllib.request.urlopen', return_value=resp):
            prov = ep.CloudProvider()
            r = prov.embed(['hello'])
        self.assertEqual(len(r.vectors), 1)
        self.assertEqual(r.vectors[0], [0.1, 0.2, 0.3])
        self.assertEqual(r.tokens, 15)
        self.assertEqual(r.cache_hits, 0)

    def test_embed_401_is_not_retriable(self):
        with patch('urllib.request.urlopen', side_effect=_http_error(401, b'invalid_key')):
            prov = ep.CloudProvider()
            with self.assertRaises(ep.EmbeddingError) as ctx:
                prov.embed(['x'])
            self.assertEqual(ctx.exception.status, 401)
            self.assertFalse(ctx.exception.retriable)

    def test_embed_500_is_retriable(self):
        with patch('urllib.request.urlopen', side_effect=_http_error(500, b'internal')):
            prov = ep.CloudProvider()
            with self.assertRaises(ep.EmbeddingError) as ctx:
                prov.embed(['x'])
            self.assertEqual(ctx.exception.status, 500)
            self.assertTrue(ctx.exception.retriable)


class CascadeTests(unittest.TestCase):
    def test_returns_primary_on_success(self):
        primary = MagicMock(spec=['name', 'embed'])
        primary.name = 'cloud'
        primary.embed.return_value = ep.EmbedResult([[1.0]], 'cloud', 5, 0, 100)
        fallback = MagicMock(spec=['name', 'embed'])
        c = ep.CascadeProvider(primary, fallback)
        r = c.embed(['x'])
        self.assertEqual(r.provider, 'cloud')
        fallback.embed.assert_not_called()

    def test_falls_back_on_retriable_error(self):
        primary = MagicMock(spec=['name', 'embed'])
        primary.name = 'cloud'
        primary.embed.side_effect = ep.EmbeddingError('boom', status=503, retriable=True)
        fallback = MagicMock(spec=['name', 'embed'])
        fallback.name = 'local'
        fallback.embed.return_value = ep.EmbedResult([[2.0]], 'local', 0, 0, 50)
        c = ep.CascadeProvider(primary, fallback)
        r = c.embed(['x'])
        self.assertEqual(r.provider, 'local')

    def test_does_not_fall_back_on_4xx(self):
        primary = MagicMock(spec=['name', 'embed'])
        primary.name = 'cloud'
        primary.embed.side_effect = ep.EmbeddingError('bad key', status=401, retriable=False)
        fallback = MagicMock(spec=['name', 'embed'])
        c = ep.CascadeProvider(primary, fallback)
        with self.assertRaises(ep.EmbeddingError) as ctx:
            c.embed(['x'])
        # Verify the ORIGINAL error propagated, not a chained "both failed".
        # A regression that made cascade swallow + re-raise would still
        # pass a bare assertRaises but lose the 401 signal for the caller.
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retriable)
        fallback.embed.assert_not_called()

    def test_chains_errors_when_both_fail(self):
        primary = MagicMock(spec=['name', 'embed'])
        primary.name = 'cloud'
        primary.embed.side_effect = ep.EmbeddingError('cloud down', status=503, retriable=True)
        fallback = MagicMock(spec=['name', 'embed'])
        fallback.name = 'local'
        fallback.embed.side_effect = ep.EmbeddingError('local also down', retriable=True)
        c = ep.CascadeProvider(primary, fallback)
        with self.assertRaises(ep.EmbeddingError) as ctx:
            c.embed(['x'])
        msg = str(ctx.exception)
        self.assertIn('cloud down', msg)
        self.assertIn('local also down', msg)


if __name__ == '__main__':
    unittest.main()
