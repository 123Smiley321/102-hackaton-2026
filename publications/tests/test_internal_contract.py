import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from publications.config import settings
from publications.monitor import ai_agent_headers, draft_reply, fetch_comments, notify_via_backend
from publications.scheduler import request_publish


class InternalContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_key = settings.publisher_internal_key
        self.old_url = settings.backend_url
        self.old_ai_key = settings.ai_agent_api_key
        self.old_ai_url = settings.ai_agent_url
        settings.publisher_internal_key = "test-publisher-key"
        settings.backend_url = "http://backend:8000"
        settings.ai_agent_api_key = "test-ai-key"
        settings.ai_agent_url = "http://ai-agent:8000/v1/generate"

    def tearDown(self):
        settings.publisher_internal_key = self.old_key
        settings.backend_url = self.old_url
        settings.ai_agent_api_key = self.old_ai_key
        settings.ai_agent_url = self.old_ai_url

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
        self.assertEqual(result.http_status, 200)
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

    async def test_not_configured_is_terminal_and_not_retried_inline(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                422,
                json={"status": "not_configured", "detail": "MAX token is missing"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.http_status, 422)
        self.assertEqual(calls, 1)

    async def test_publish_failed_is_distinct_from_publish_unknown(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422,
                json={"status": "publish_failed", "detail": "MAX API HTTP 401"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await request_publish(client, 42)

        self.assertEqual(result.status, "publish_failed")
        self.assertNotEqual(result.status, "publish_unknown")

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

    def test_ai_agent_key_is_scoped_to_internal_header(self):
        self.assertEqual(ai_agent_headers(), {"X-Internal-Key": "test-ai-key"})
        settings.ai_agent_api_key = ""
        self.assertEqual(ai_agent_headers(), {})

    async def test_comment_draft_sends_ai_internal_key(self):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"text": "Спасибо за вопрос!"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json, headers):
                calls.append((url, json, headers))
                return FakeResponse()

        company = SimpleNamespace(
            id=1, external_id="company-a", name="Компания А", description="",
            tone_of_voice="", products="", target_audience="", advantages="",
        )
        with patch("publications.monitor.httpx.AsyncClient", FakeClient):
            text = await draft_reply(company, "Когда будет в наличии?")

        self.assertEqual(text, "Спасибо за вопрос!")
        self.assertEqual(calls[0][2], {"X-Internal-Key": "test-ai-key"})
        self.assertEqual(calls[0][1]["company"]["company_id"], "company-a")
        self.assertEqual(calls[0][1]["knowledge_base"], [])


if __name__ == "__main__":
    unittest.main()
