"""
Unit tests for code/cloud_cli.py — config.env preservation + verify behavior.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'code'))

import cloud_cli  # noqa: E402


def _http_error(code: int, body: bytes = b''):
    return urllib.error.HTTPError(
        url='http://x', code=code, msg='boom',
        hdrs=None, fp=io.BytesIO(body),  # type: ignore
    )


class ConfigEnvTests(unittest.TestCase):
    """`_set_config_env` MUST preserve comments + ordering of unrelated lines."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_root = os.environ.get('CLAUDE_DEJAVU_DATA_ROOT')
        os.environ['CLAUDE_DEJAVU_DATA_ROOT'] = self.tmp.name

    def tearDown(self):
        if self._orig_root is None:
            os.environ.pop('CLAUDE_DEJAVU_DATA_ROOT', None)
        else:
            os.environ['CLAUDE_DEJAVU_DATA_ROOT'] = self._orig_root
        self.tmp.cleanup()

    def _write_initial(self, content: str) -> Path:
        p = cloud_cli._config_env_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_sets_new_keys_appending(self):
        p = self._write_initial("# comment\nPG_HOST=localhost\n")
        cloud_cli._set_config_env({'DEJAVU_EMBED_MODE': 'cloud'})
        text = p.read_text()
        self.assertIn('# comment', text)
        self.assertIn('PG_HOST=localhost', text)
        self.assertIn('DEJAVU_EMBED_MODE=cloud', text)

    def test_replaces_existing_in_place_preserving_order(self):
        p = self._write_initial(
            "# header\n"
            "PG_HOST=localhost\n"
            "DEJAVU_EMBED_MODE=local\n"
            "PG_PORT=5450\n"
        )
        cloud_cli._set_config_env({'DEJAVU_EMBED_MODE': 'cloud'})
        lines = p.read_text().splitlines()
        # Order preserved: comment → PG_HOST → mode → PG_PORT
        self.assertEqual(lines[0], '# header')
        self.assertEqual(lines[1], 'PG_HOST=localhost')
        self.assertEqual(lines[2], 'DEJAVU_EMBED_MODE=cloud')
        self.assertEqual(lines[3], 'PG_PORT=5450')

    def test_load_config_env_strips_comments_and_blanks(self):
        self._write_initial("# top comment\n\nA=1\n  B=2  \n# tail\n")
        cfg = cloud_cli._load_config_env()
        self.assertEqual(cfg['A'], '1')
        self.assertEqual(cfg['B'], '2')
        self.assertNotIn('# top comment', cfg)


class VerifyKeyTests(unittest.TestCase):
    def setUp(self):
        os.environ['DEJAVU_CLOUD_URL'] = 'https://worker.test'

    def tearDown(self):
        os.environ.pop('DEJAVU_CLOUD_URL', None)

    def test_wrong_format_rejects_without_network(self):
        ok, detail = cloud_cli._verify_key('plain-string')
        self.assertFalse(ok)
        self.assertIn('wrong format', detail)

    def test_401_returns_key_rejected(self):
        with patch('urllib.request.urlopen', side_effect=_http_error(401, b'invalid')):
            ok, detail = cloud_cli._verify_key('dvk_live_aaaaaaaaaaaaaaaaaaaaaaaa')
        self.assertFalse(ok)
        self.assertIn('401', detail)

    def test_402_quota_exhausted(self):
        with patch('urllib.request.urlopen', side_effect=_http_error(402, b'cap')):
            ok, detail = cloud_cli._verify_key('dvk_live_aaaaaaaaaaaaaaaaaaaaaaaa')
        self.assertFalse(ok)
        self.assertIn('quota', detail)

    def test_200_returns_ok(self):
        class R:
            def __init__(self, body):
                self._body = body
                self.status = 200
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        import json as _json
        with patch(
            'urllib.request.urlopen',
            return_value=R(_json.dumps({'quota_remaining': 100}).encode()),
        ):
            ok, detail = cloud_cli._verify_key('dvk_live_aaaaaaaaaaaaaaaaaaaaaaaa')
        self.assertTrue(ok)
        self.assertIn('100', detail)


if __name__ == '__main__':
    unittest.main()
