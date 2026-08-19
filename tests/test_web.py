from __future__ import annotations

import socket
import unittest

from aiohttp.test_utils import TestClient, TestServer

from sefbot.web import (
    ReadinessState,
    WebConfigurationError,
    WebService,
    create_app,
)


class WebApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = ReadinessState()
        app = create_app(
            privacy_contact="privacy@example.test", readiness=self.state
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_legal_pages_are_html_and_hardened(self) -> None:
        for path in ("/sefbot", "/sefbot/terms", "/sefbot/privacy"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, "text/html")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(
                    response.headers["X-Content-Type-Options"], "nosniff"
                )
                self.assertEqual(response.headers["Server"], "OpSef")
                self.assertIn(
                    "max-age=31536000",
                    response.headers["Strict-Transport-Security"],
                )
                self.assertIn(
                    "frame-ancestors 'none'",
                    response.headers["Content-Security-Policy"],
                )
                self.assertEqual(
                    response.headers["Cache-Control"], "public, max-age=300"
                )
                body = await response.text()
                self.assertIn("OpSef", body)
                if path != "/sefbot":
                    self.assertIn("Version 2.0", body)
                    self.assertIn("effective 19 August 2026", body)

    async def test_health_does_not_claim_dependency_readiness(self) -> None:
        health = await self.client.get("/healthz")
        self.assertEqual(health.status, 200)
        self.assertEqual(await health.json(), {"status": "ok"})
        self.assertEqual(health.headers["Cache-Control"], "no-store")
        self.assertEqual(health.headers["X-Robots-Tag"], "noindex, nofollow")

        not_ready = await self.client.get("/readyz")
        self.assertEqual(not_ready.status, 503)
        self.assertEqual(
            await not_ready.json(),
            {"status": "not_ready", "discord": False, "database": False},
        )

        self.state.discord = True
        self.state.database = True
        ready = await self.client.get("/readyz")
        self.assertEqual(ready.status, 200)
        self.assertEqual(
            await ready.json(),
            {"status": "ready", "discord": True, "database": True},
        )

    async def test_compatibility_routes_redirect_without_reflecting_input(self) -> None:
        cases = {
            "/sefbot/": "/sefbot",
            "/sefbot/tos": "/sefbot/terms",
            "/opsef-tos.html": "/sefbot/terms",
            "/opsef-privacy.html": "/sefbot/privacy",
        }
        for path, location in cases.items():
            with self.subTest(path=path):
                response = await self.client.get(path, allow_redirects=False)
                self.assertEqual(response.status, 308)
                self.assertEqual(response.headers["Location"], location)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_unsupported_methods_and_unknown_routes_are_safe(self) -> None:
        method_response = await self.client.post("/sefbot", data=b"ignored")
        self.assertEqual(method_response.status, 405)
        self.assertEqual(method_response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(method_response.headers["Cache-Control"], "no-store")

        missing_response = await self.client.get("/not-a-route")
        self.assertEqual(missing_response.status, 404)
        self.assertEqual(missing_response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(missing_response.headers["Cache-Control"], "no-store")

    async def test_contact_is_escaped(self) -> None:
        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact='<script>alert("x")</script>',
                    readiness=lambda: True,
                )
            )
        )
        await client.start_server()
        try:
            response = await client.get("/sefbot/privacy")
            body = await response.text()
            self.assertNotIn('<script>alert("x")</script>', body)
            self.assertIn("&lt;script&gt;", body)
        finally:
            await client.close()

    async def test_readiness_failure_is_sanitized(self) -> None:
        async def broken_readiness() -> bool:
            raise RuntimeError("database-password-should-not-leak")

        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact="privacy@example.test",
                    readiness=broken_readiness,
                )
            )
        )
        await client.start_server()
        try:
            with self.assertLogs("sefbot.web", level="WARNING"):
                response = await client.get("/readyz")
            self.assertEqual(response.status, 503)
            body = await response.text()
            self.assertNotIn("database-password", body)
            self.assertEqual(await response.json(), {"status": "not_ready"})
        finally:
            await client.close()

    async def test_readiness_rejects_unexpected_component_names(self) -> None:
        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact="privacy@example.test",
                    readiness=lambda: {"provider_password": True},
                )
            )
        )
        await client.start_server()
        try:
            with self.assertLogs("sefbot.web", level="WARNING"):
                response = await client.get("/readyz")
            self.assertEqual(response.status, 503)
            body = await response.text()
            self.assertNotIn("provider_password", body)
            self.assertEqual(await response.json(), {"status": "not_ready"})
        finally:
            await client.close()

    async def test_web_service_lifecycle_is_idempotent(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        service = WebService(
            privacy_contact="privacy@example.test",
            readiness=lambda: True,
            host="127.0.0.1",
            port=port,
        )
        await service.start()
        await service.start()
        await service.close()
        await service.close()


class WebConfigurationTests(unittest.TestCase):
    def test_privacy_contact_is_required(self) -> None:
        for contact in ("", "privacy@example.test\nspoofed", None):
            with self.subTest(contact=contact):
                with self.assertRaises(WebConfigurationError):
                    create_app(privacy_contact=contact)  # type: ignore[arg-type]

    def test_readiness_provider_must_be_callable(self) -> None:
        with self.assertRaises(WebConfigurationError):
            create_app(
                privacy_contact="privacy@example.test",
                readiness=True,  # type: ignore[arg-type]
            )

    def test_listener_address_is_validated(self) -> None:
        for host, port in (("host\nname", 8080), ("127.0.0.1", 0), ("localhost", "x")):
            with self.subTest(host=host, port=port):
                with self.assertRaises(WebConfigurationError):
                    WebService(
                        privacy_contact="privacy@example.test",
                        readiness=lambda: True,
                        host=host,
                        port=port,
                    )

if __name__ == "__main__":
    unittest.main()
