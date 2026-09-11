import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from datetime import datetime, timezone


loader = importlib.machinery.SourceFileLoader(
  "collector", str(Path(__file__).parent / "collectors/omarchy-agent-usage-2ba")
)
collector = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(collector)


def summary():
  day = datetime.now(timezone.utc).date().isoformat()
  return {"scope": "api_key", "timezone": "UTC", "total_requests": 3, "total_tokens": 250,
    "daily_stats": [{"date": day, "requests": 2, "tokens": 200},
                    {"date": day, "requests": 1, "tokens": 50}]}


class CollectorTests(unittest.TestCase):
  def test_user_rolling_quota(self):
    quota = {"scope": "user", "enforcement": "active", "plan": {"name": "Pro", "inherited": True},
      "limits": [
        {"scope": "user", "metric": "requests", "window_type": "rolling", "window_seconds": 18000,
         "limit": 4500, "used": 1200, "remaining": 3300, "next_release_at": "2026-09-11T12:30:00Z"},
        {"scope": "user", "metric": "concurrent_requests", "limit": 8, "used": 2, "remaining": 6}]}
    record = collector.summarize({**summary(), "quota": quota})
    self.assertEqual(record["todayTotalTokens"], 250)
    self.assertEqual(record["tierLabel"], "Pro")
    self.assertAlmostEqual(record["limits"][0]["percent"], 1200 / 4500)
    self.assertEqual(record["limits"][0]["title"], "User requests (5h rolling)")
    self.assertEqual(record["limits"][0]["resetsAt"], "")
    self.assertEqual(record["limits"][1]["percent"], 0.25)
    self.assertEqual(record["usageStatusText"], "")
    self.assertEqual(record["authHelpText"], "")
    self.assertIn("all keys", record["usageSummaryText"])
    self.assertIn("next capacity Sep 11 12:30 UTC", record["usageSummaryText"])
    quota["limits"][0].update(used=5000, remaining=0, next_release_at=None)
    record = collector.summarize({**summary(), "quota": quota})
    self.assertEqual(record["limits"][0]["percent"], 1)
    self.assertNotIn("next capacity", record["usageSummaryText"])
    unlimited = collector.summarize({**summary(), "quota": {"scope": "user", "enforcement": "unlimited", "limits": []}})
    self.assertEqual(unlimited["limits"], [])
    self.assertEqual(unlimited["usageStatusText"], "")
    self.assertIn("no user rate limit", unlimited["usageSummaryText"])
    for invalid in [None, {**quota, "scope": "organization"}, {**quota, "enforcement": "unavailable"}]:
      with self.subTest(invalid=invalid), self.assertRaises(collector.UsageError):
        collector.summarize({**summary(), "quota": invalid})

  def test_key_scoped_usage(self):
    with patch.object(collector, "api_key", return_value="sk-test"), patch.object(collector, "request", return_value=summary()) as request:
      record = collector.scan()
    request.assert_called_once_with("https://api.2ba.ai/v1/usage", "sk-test")
    self.assertTrue(record["ready"])
    self.assertEqual(record["usageStatusText"], "")
    self.assertEqual(record["todayTotalTokens"], 250)
    self.assertEqual(record["recentDays"][-1]["messageCount"], 250)
    self.assertEqual(record["scope"], "account")
    self.assertFalse(record["hasPromptStats"])
    self.assertEqual(record["limits"], [])
    self.assertNotIn("sk-test", json.dumps(record))
    self.assertEqual(len(record["recentDays"]), 7)

  def test_empty_and_invalid_responses(self):
    empty = {**summary(), "total_requests": 0, "total_tokens": 0, "daily_stats": []}
    self.assertEqual(collector.summarize(empty)["todayTotalTokens"], 0)
    for bad in [{}, {**summary(), "scope": "organization"}, {**summary(), "total_tokens": float("nan")},
                {**summary(), "daily_stats": [{"date": "bad", "tokens": 5}]}]:
      with self.subTest(bad=bad), self.assertRaises(collector.UsageError):
        collector.summarize(bad)

  def test_key_precedence(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "key"
      path.write_text("a" * 64 + "\n")
      with patch.object(collector, "key_path", return_value=path), patch.dict(os.environ, {}, clear=True):
        self.assertEqual(collector.api_key(), "a" * 64)
        with patch.dict(os.environ, {"TWOBA_API_KEY": "sk-env"}):
          self.assertEqual(collector.api_key(), "sk-env")
        path.unlink()
        self.assertEqual(collector.api_key(), "")

  def test_transport_credentials_and_errors(self):
    class Response(io.BytesIO):
      pass
    with patch.object(collector.urllib.request, "build_opener") as opener:
      opener.return_value.open.return_value = Response(b'{"ok":true}')
      collector.request("https://api.2ba.ai/v1/usage", "sk-secret")
      req = opener.return_value.open.call_args.args[0]
      self.assertEqual(req.get_header("Authorization"), "Bearer sk-secret")
      for url in ["http://api.2ba.ai/v1/usage", "https://api.2ba.ai.evil.test/", "https://evil.test/", "https://user:pass@2ba.ai/"]:
        with self.assertRaises(collector.UsageError):
          collector.request(url, "sk-secret")
      for status in (301, 401, 403, 404, 500):
        opener.return_value.open.side_effect = urllib.error.HTTPError(req.full_url, status, "sk-secret", {}, io.BytesIO(b"sk-secret"))
        with self.assertRaises(collector.UsageError) as caught:
          collector.request(req.full_url, "sk-secret")
        self.assertNotIn("sk-secret", str(caught.exception))
      opener.return_value.open.side_effect = urllib.error.HTTPError(req.full_url, 400, "pending", {}, io.BytesIO(b'{"error":"authorization_pending"}'))
      self.assertIsNone(collector.request("https://2ba.ai/api/auth/device/token", body={"device_code": "secret"}, pending=True))
    self.assertIsNone(collector.NoRedirect().redirect_request(req, None, 302, "", {}, "https://evil.test"))

  def test_pairing_saves_private_key_and_handles_pending(self):
    session = {"device_code": "device-secret", "user_code": "ABCD2345",
      "verification_url": "https://2ba.ai/link?code=ABCD2345", "expires_in": 600}
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "2ba/2BA_API_KEY"
      out = io.StringIO()
      key = "b" * 64
      with patch.object(collector, "key_path", return_value=path), patch.object(collector, "request", side_effect=[session, None, {"api_key": key}]) as request, patch.object(collector.webbrowser, "open"), patch.object(collector.time, "sleep"), contextlib.redirect_stdout(out):
        collector.login()
      self.assertEqual(path.read_text(), key)
      self.assertEqual(path.stat().st_mode & 0o777, 0o600)
      self.assertNotIn(key, out.getvalue())
      self.assertNotIn("device-secret", out.getvalue())
      self.assertEqual(request.call_args.kwargs["body"], {"device_code": "device-secret"})
      self.assertEqual(list(path.parent.iterdir()), [path])
      with patch.object(collector, "key_path", return_value=path), patch.object(collector, "request", return_value={**session, "verification_url": "https://evil.test/link"}), self.assertRaises(collector.UsageError):
        collector.login()
      self.assertEqual(path.read_text(), key)
      self.assertEqual(list(path.parent.iterdir()), [path])
      with patch.object(collector, "key_path", return_value=path), patch.object(collector, "request", side_effect=[session, None]), patch.object(collector.webbrowser, "open"), patch.object(collector.time, "sleep"), patch.object(collector.time, "monotonic", side_effect=[0, 0, 601]), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(collector.UsageError, "timed out"):
        collector.login()
      self.assertEqual(path.read_text(), key)
      self.assertEqual(list(path.parent.iterdir()), [path])

  def test_scan_failure_is_json_and_exit_zero(self):
    with patch.object(collector, "scan", side_effect=collector.UsageError("Endpoint unavailable")), patch.object(collector.sys, "argv", ["collector"]), contextlib.redirect_stdout(io.StringIO()) as out:
      self.assertEqual(collector.main(), 0)
    record = json.loads(out.getvalue())
    self.assertFalse(record["ready"])
    self.assertEqual(record["authHelpText"], "Endpoint unavailable")


if __name__ == "__main__":
  unittest.main()
