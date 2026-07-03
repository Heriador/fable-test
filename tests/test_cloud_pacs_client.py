"""
Tests for CloudPACSClient – uses httpx's MockTransport so no real network calls.
"""
from __future__ import annotations

import pytest
import httpx
from unittest.mock import MagicMock, patch


class TestBuildMultipart:
    def test_single_instance(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        client = CloudPACSClient()
        instances = [b"\x00\x01\x02\x03"]
        headers, body = client._build_multipart(instances)

        assert "multipart/related" in headers["Content-Type"]
        assert b"Content-Type: application/dicom" in body
        assert instances[0] in body

    def test_multiple_instances(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        client = CloudPACSClient()
        instances = [b"instance1", b"instance2", b"instance3"]
        headers, body = client._build_multipart(instances)

        for inst in instances:
            assert inst in body

    def test_closing_delimiter_present(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        client = CloudPACSClient()
        _, body = client._build_multipart([b"data"])
        assert b"--" in body
        assert body.endswith(b"--\r\n")


class TestStowURL:
    def test_url_without_study_uid(self):
        from src.clients.cloud_pacs_client import CloudPACSClient
        from src.middleware.config import settings

        client = CloudPACSClient()
        url = client._build_stow_url(None)

        assert str(settings.cloud_pacs_url).rstrip("/") in url
        assert url.endswith("/studies")

    def test_url_with_study_uid(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        client = CloudPACSClient()
        uid = "1.2.3.4.5"
        url = client._build_stow_url(uid)

        assert uid in url


class TestStoreInstances:
    def test_successful_upload(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.content = b'{"status": "ok"}'
        mock_response.json.return_value = {"status": "ok"}

        client = CloudPACSClient()
        with patch.object(client, "_post_with_retry", return_value=mock_response):
            result = client.store_instances([b"dicom_data"])

        assert result == {"status": "ok"}

    def test_empty_response_body(self):
        from src.clients.cloud_pacs_client import CloudPACSClient

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 202
        mock_response.content = b""

        client = CloudPACSClient()
        with patch.object(client, "_post_with_retry", return_value=mock_response):
            result = client.store_instances([b"dicom_data"])

        assert result == {}
