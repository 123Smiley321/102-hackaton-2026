"""Генераторы картинок на подставных серверах: Kandinsky (FusionBrain) и OpenAI-совместимый."""
import asyncio
import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image  # noqa: E402

import marketer.images as im  # noqa: E402
from marketer.config import Settings  # noqa: E402

buf = io.BytesIO()
Image.new("RGB", (16, 9), "blue").save(buf, "PNG")
PNG_B64 = base64.b64encode(buf.getvalue()).decode()


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(BaseHTTPRequestHandler):
    def ok(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, *a):
        pass


class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "img.png"
        orig = im.kandinsky_image

        async def fast(p, o, s):
            return await orig(p, o, s, delay=0.01)
        im.kandinsky_image = fast
        self.addCleanup(setattr, im, "kandinsky_image", orig)

    def kandinsky_settings(self, srv):
        s = Settings()
        s.image_provider, s.kandinsky_api_key, s.kandinsky_secret_key = "kandinsky", "AK", "SK"
        s.kandinsky_base_url = f"http://127.0.0.1:{srv.server_port}/"
        return s

    def test_kandinsky_full_cycle(self):
        seen = []

        class H(Base):
            polls = 0

            def do_GET(self):
                seen.append((self.path, self.headers["X-Key"], self.headers["X-Secret"]))
                if self.path.endswith("/pipelines"):
                    return self.ok([{"id": "pipe-1"}])
                H.polls += 1
                self.ok({"status": "PROCESSING"} if H.polls < 2 else
                        {"status": "DONE", "result": {"files": [PNG_B64], "censored": False}})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                seen.append((self.path, "pipe-1" in body, "glass facade" in body))
                self.ok({"uuid": "job-7"})
        path, engine = asyncio.run(im.generate_image("glass facade", "T", self.out, self.kandinsky_settings(serve(H))))
        self.assertEqual(engine, "kandinsky (FusionBrain)")
        self.assertEqual(Image.open(path).size, (16, 9))
        self.assertIn(("/key/api/v1/pipelines", "Key AK", "Secret SK"), seen)
        self.assertIn(("/key/api/v1/pipeline/run", True, True), seen)

    def test_kandinsky_failure_is_labelled(self):
        class H(Base):
            def do_GET(self):
                self.ok([{"id": "p"}] if self.path.endswith("/pipelines") else {"status": "FAIL"})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.ok({"uuid": "u"})
        path, engine = asyncio.run(im.generate_image("x", "Тест", self.out, self.kandinsky_settings(serve(H))))
        self.assertTrue(path.exists())
        self.assertIn("генератор недоступен", engine)

    def test_pollinations_no_key(self):
        seen = {}
        jpeg = io.BytesIO()
        Image.new("RGB", (32, 18), "green").save(jpeg, "JPEG")

        class H(Base):
            def do_GET(self):
                seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(jpeg.getvalue())
        srv = serve(H)
        s = Settings()
        s.image_provider, s.pollinations_api_key = "pollinations", ""
        s.pollinations_base_url = f"http://127.0.0.1:{srv.server_port}"
        path, engine = asyncio.run(im.generate_image("glass facade, no text", "T", self.out, s))
        self.assertEqual(engine, "pollinations (flux)")
        self.assertEqual(Image.open(path).format, "PNG")
        self.assertTrue(seen["path"].startswith("/prompt/glass%20facade"))
        self.assertIn("width=1024", seen["path"])
        self.assertIsNone(seen["auth"])  # работает без ключа

    def test_pollinations_retries_on_429(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(jpeg, "JPEG")
        calls = []

        class H(Base):
            def do_GET(self):
                calls.append(1)
                if len(calls) == 1:  # первый раз — «слишком часто»
                    self.send_response(429)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(jpeg.getvalue())
        srv = serve(H)
        s = Settings()
        s.image_provider, s.pollinations_api_key, s.pollinations_retry_wait = "pollinations", "", 0.01
        s.pollinations_base_url = f"http://127.0.0.1:{srv.server_port}"
        _, engine = asyncio.run(im.generate_image("x", "T", self.out, s))
        self.assertEqual((engine, len(calls)), ("pollinations (flux)", 2))

    def test_openai_compatible_uses_image_key(self):
        seen = {}

        class H(Base):
            def do_POST(self):
                seen["auth"], seen["path"] = self.headers["Authorization"], self.path
                self.rfile.read(int(self.headers["Content-Length"]))
                self.ok({"data": [{"b64_json": PNG_B64}]})
        srv = serve(H)
        s = Settings()
        s.image_provider, s.image_api_key, s.openai_api_key = "openai", "img-key", "text-key"
        s.image_base_url = f"http://127.0.0.1:{srv.server_port}/v1"
        _, engine = asyncio.run(im.generate_image("x", "T", self.out, s))
        self.assertEqual((seen["auth"], seen["path"]), ("Bearer img-key", "/v1/images/generations"))
        self.assertIn("gpt-image-1", engine)


if __name__ == "__main__":
    unittest.main()
