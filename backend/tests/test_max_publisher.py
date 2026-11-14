import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base
from app.main import (
    publish_approved_publication,
    resolve_uncertain_publication,
    revoke_platform_credentials,
    update_company,
)
from app.models import Company, Publication, Task
from app.publisher import (
    PublicationNotConfiguredError,
    PublicationRejectedError,
    publish_max,
)
from app.schemas import CompanyOut, CompanyUpdate
from app.security import decrypt_secret, encrypt_secret


class MaxPublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_encryption_key = settings.encryption_key
        settings.encryption_key = Fernet.generate_key().decode()
        self.token = "test-max-token-not-a-real-secret"
        self.company = SimpleNamespace(
            max_access_token=encrypt_secret(self.token),
            max_chat_id="-123456789",
        )
        self.publication = SimpleNamespace(
            text="Тестовый пост для MAX",
            image_url=None,
        )

    def tearDown(self):
        settings.encryption_key = self.old_encryption_key

    async def test_text_publish_uses_official_contract_and_returns_real_mid(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.method == "POST"
            assert request.url.path == "/messages"
            assert request.url.params["chat_id"] == "-123456789"
            assert request.headers["Authorization"] == self.token
            assert "access_token" not in request.url.params
            assert json.loads(request.content) == {"text": "Тестовый пост для MAX"}
            return httpx.Response(
                200,
                json={
                    "message": {
                        "message": {"mid": "mid.real.001", "text": "Тестовый пост для MAX"},
                        "url": "https://max.ru/channel/example/1",
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await publish_max(self.company, self.publication, client=client)

        self.assertEqual(result, ("mid.real.001", "https://max.ru/channel/example/1"))
        self.assertEqual(len(calls), 1)

    async def test_image_is_sent_as_official_direct_url_attachment(self):
        image_url = "https://images.example.com/public/post.png"
        self.publication.image_url = image_url

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["text"] == self.publication.text
            assert body["attachments"] == [
                {"type": "image", "payload": {"url": image_url}}
            ]
            return httpx.Response(200, json={"message": {"message": {"mid": "mid.image.1"}}})

        with patch("app.publisher._validate_image_url", new=AsyncMock()) as validate:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                post_id, post_url = await publish_max(
                    self.company, self.publication, client=client
                )

        self.assertEqual(post_id, "mid.image.1")
        self.assertIsNone(post_url)
        validate.assert_awaited_once_with(image_url)

    async def test_missing_credentials_never_calls_network(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        self.company.max_access_token = None
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicationNotConfiguredError):
                await publish_max(self.company, self.publication, client=client)
        self.assertEqual(calls, 0)

    async def test_invalid_chat_id_never_calls_network(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        self.company.max_chat_id = "not-an-int64-id"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicationNotConfiguredError):
                await publish_max(self.company, self.publication, client=client)
        self.assertEqual(calls, 0)

    async def test_http_401_is_definite_rejection_and_redacts_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"code": "unauthorized", "message": f"bad token {self.token}"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicationRejectedError) as caught:
                await publish_max(self.company, self.publication, client=client)

        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn(self.token, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    async def test_http_200_success_false_is_not_published(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": False, "code": "chat.denied", "message": "denied"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicationRejectedError):
                await publish_max(self.company, self.publication, client=client)

    async def test_http_200_without_mid_is_not_fake_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {"message": {}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RuntimeError, "message.message.mid"):
                await publish_max(self.company, self.publication, client=client)

    async def test_transport_failure_propagates_without_inline_retry(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("synthetic network failure", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.ConnectError):
                await publish_max(self.company, self.publication, client=client)
        self.assertEqual(calls, 1)

    async def test_http_503_remains_ambiguous_and_has_no_inline_retry(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="synthetic unavailable")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await publish_max(self.company, self.publication, client=client)
        self.assertEqual(calls, 1)


class MaxPublicationStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_publisher_key = settings.publisher_internal_key
        self.old_encryption_key = settings.encryption_key
        settings.publisher_internal_key = "test-publisher-key"
        settings.encryption_key = Fernet.generate_key().decode()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.session = self.sessions()
        self.company = Company(
            external_id="max-test-company",
            name="MAX Test Company",
            enabled_platforms=["max", "vk"],
            max_chat_id="-123456789",
            max_access_token=encrypt_secret("test-max-token"),
            vk_group_id="123",
            vk_access_token=encrypt_secret("test-vk-token"),
        )
        self.session.add(self.company)
        await self.session.flush()
        self.task = Task(company_id=self.company.id, prompt="MAX test", source="test")
        self.session.add(self.task)
        await self.session.flush()
        self.publication = Publication(
            company_id=self.company.id,
            task_id=self.task.id,
            text="MAX state test",
            platform="max",
            status="approved",
            idempotency_key="max-state-test-1",
        )
        self.session.add(self.publication)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        settings.publisher_internal_key = self.old_publisher_key
        settings.encryption_key = self.old_encryption_key

    async def test_not_configured_is_persisted_and_can_be_explicitly_requeued(self):
        with (
            patch(
                "app.main.publish_to_platform",
                new=AsyncMock(side_effect=PublicationNotConfiguredError("MAX not_configured")),
            ) as remote,
            patch("app.main.notify_operator_safely", new=AsyncMock(return_value=True)),
        ):
            response = await publish_approved_publication(
                self.publication.id,
                x_publisher_key="test-publisher-key",
                db=self.session,
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.body)["status"], "not_configured")
        self.assertEqual(self.publication.status, "not_configured")
        self.assertIsNone(self.publication.published_post_id)
        remote.assert_awaited_once()

        resolved = await resolve_uncertain_publication(
            self.publication.id, {"action": "retry"}, db=self.session
        )
        self.assertEqual(resolved["status"], "approved")
        self.assertIsNone(self.publication.publish_error)

    async def test_platform_rejection_is_publish_failed_not_unknown(self):
        with (
            patch(
                "app.main.publish_to_platform",
                new=AsyncMock(side_effect=PublicationRejectedError("MAX API HTTP 401")),
            ),
            patch("app.main.notify_operator_safely", new=AsyncMock(return_value=True)),
        ):
            response = await publish_approved_publication(
                self.publication.id,
                x_publisher_key="test-publisher-key",
                db=self.session,
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.body)["status"], "publish_failed")
        self.assertEqual(self.publication.status, "publish_failed")
        self.assertNotEqual(self.publication.status, "publish_unknown")
        self.assertIsNone(self.publication.published_post_id)

    async def test_publish_unknown_still_requires_explicit_confirmation(self):
        self.publication.status = "publish_unknown"
        await self.session.commit()

        with self.assertRaises(HTTPException) as caught:
            await resolve_uncertain_publication(
                self.publication.id, {"action": "retry"}, db=self.session
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.publication.status, "publish_unknown")

        resolved = await resolve_uncertain_publication(
            self.publication.id,
            {"action": "retry", "confirmed_not_published": True},
            db=self.session,
        )
        self.assertEqual(resolved["status"], "approved")

    async def test_company_output_hides_token_and_revoke_clears_only_max(self):
        serialized = CompanyOut.model_validate(self.company).model_dump()
        self.assertNotIn("max_access_token", serialized)
        self.assertNotIn("vk_access_token", serialized)
        self.assertEqual(serialized["max_chat_id"], "-123456789")

        result = await revoke_platform_credentials(
            self.company.id, {"platform": "max"}, db=self.session
        )
        self.assertEqual(result["status"], "revoked")
        self.assertIsNone(self.company.max_access_token)
        self.assertIsNone(self.company.max_chat_id)
        self.assertEqual(self.company.enabled_platforms, ["vk"])
        self.assertIsNotNone(self.company.vk_access_token)
        self.assertEqual(self.company.vk_group_id, "123")


    async def test_max_token_update_is_encrypted_and_never_returned(self):
        raw_token = "updated-max-token-not-a-real-secret"
        result = await update_company(
            self.company.id,
            CompanyUpdate(max_access_token=raw_token, max_chat_id="-987654321"),
            db=self.session,
        )

        self.assertNotEqual(self.company.max_access_token, raw_token)
        self.assertEqual(decrypt_secret(self.company.max_access_token), raw_token)
        self.assertEqual(self.company.max_chat_id, "-987654321")
        serialized = CompanyOut.model_validate(result).model_dump()
        self.assertNotIn("max_access_token", serialized)
        self.assertNotIn("vk_access_token", serialized)
        self.assertNotIn(raw_token, json.dumps(serialized))


if __name__ == "__main__":
    unittest.main()
