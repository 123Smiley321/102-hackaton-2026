import unittest

import httpx

from publications.config import settings
from publications.monitor import fetch_comments, notify_via_backend
from publications.scheduler import request_publish


class InternalContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_key = settings.publisher_internal_key
        self.old_url = settings.backend_url
        settings.publisher_internal_key = "test-publisher-key"
        settings.backend_url = "http://backend:8000"

    def tearDown(self):
        settings.publisher_internal_key = self.old_key
        settings.backend_url = self.old_url

    async def test_publish_uses_backend_contract(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.method == "POST"
            assert request.url.path == "/internal/publications/42/publish"
            assert request.headers["X-Publisher-Key"] == "test-publisher-key"
            return httpx.Response(
                200,
                json={
                    "status": "published",
                    "publication_id": 42,
                    "published_post_id": "9001",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "published")
        self.assertEqual(result.payload["published_post_id"], "9001")
        self.assertEqual(len(calls), 1)

    async def test_already_published_is_terminal(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"status": "already_published"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "already_published")
        self.assertEqual(calls, 1)

    async def test_conflict_is_not_bypassed(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(409, json={"detail": "status=publishing"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.http_status, 409)
        self.assertEqual(calls, 1)

    async def test_502_requires_manual_reconciliation_without_retry(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(502, json={"detail": "manual check required"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "publish_unknown")
        self.assertEqual(result.http_status, 502)
        self.assertEqual(calls, 1)

    async def test_transport_failure_has_no_inline_retry(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("test network failure", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "dispatch_unknown")
        self.assertIsNone(result.http_status)
        self.assertEqual(calls, 1)

    async def test_comments_are_read_through_backend(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/internal/publications/42/comments"
            assert request.headers["X-Publisher-Key"] == "test-publisher-key"
            return httpx.Response(
                200,
                json={
                    "comments": [
                        {"external_comment_id": "7", "author_id": "8", "text": "Вопрос"}
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            comments = await fetch_comments(client, 42)

        self.assertEqual(comments[0]["external_comment_id"], "7")

    async def test_notification_is_sent_through_backend(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/internal/publications/42/notify"
            assert request.headers["X-Publisher-Key"] == "test-publisher-key"
            assert request.content == b'{"text":"Moderation alert"}'
            return httpx.Response(200, json={"status": "sent"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await notify_via_backend(client, 42, "Moderation alert")


if __name__ == "__main__":
    unittest.main()
