"""
Tests for src/watchdog/transport.py — webhook transport (mocked HTTP).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.watchdog.transport import WebhookTransport, _post


class TestPost:
    @patch("src.watchdog.transport.urllib.request.urlopen")
    def test_successful_post(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = _post("http://example.com/hook", {"title": "test"})
        assert result == {"ok": True}

    @patch("src.watchdog.transport.urllib.request.urlopen")
    def test_failed_post(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection error")
        result = _post("http://example.com/hook", {"title": "test"})
        assert result["ok"] is False


class TestWebhookTransport:
    @patch.dict("os.environ", {"SWAXS_SLACK_WEBHOOK_URL": "http://example.com/hook"})
    @patch("src.watchdog.transport._post")
    def test_send_queues_message(self, mock_post):
        transport = WebhookTransport()
        transport.send("title", "text", "info")
        transport.close(timeout=0.5)
        # Give the worker a moment to process
        import time
        time.sleep(0.1)
        assert mock_post.called

    @patch.dict("os.environ", {}, clear=False)
    def test_no_webhook_url(self):
        with patch.dict("os.environ", {"SWAXS_SLACK_WEBHOOK_URL": ""}):
            transport = WebhookTransport()
            assert transport.webhook_url == ""
            transport.send("title", "text", "info")
            transport.close()

    def test_transport_with_min_interval(self):
        with patch.dict("os.environ", {"SWAXS_SLACK_WEBHOOK_URL": "http://example.com"}):
            transport = WebhookTransport(min_interval_s=1.0)
            assert transport.min_interval_s == 1.0
