"""Защита от скрытой подмены: провайдер codex работает только при входе через аккаунт ChatGPT."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marketer.config import Settings  # noqa: E402
from marketer.llm import CodexChatGPTLLM, LLMError, make_llm  # noqa: E402


def fake_codex(status_line: str) -> str:
    """Подставной codex: печатает заданный статус входа, на exec пишет ответ в файл -o."""
    d = tempfile.mkdtemp()
    p = Path(d) / "codex"
    p.write_text(f"""#!/usr/bin/env python3
import sys
a = sys.argv[1:]
if a[:2] == ["login", "status"]:
    print({status_line!r}); sys.exit(0 if "Logged in" in {status_line!r} else 1)
if a and a[0] == "exec":
    assert a[-1] == "-", "задание должно идти через stdin"
    prompt = sys.stdin.read()
    out = a[a.index("--output-last-message") + 1]
    open(out, "w", encoding="utf-8").write("ПРОВЕРКА-CHATGPT-OK" if "ПРОВЕРКА" in prompt or "u" in prompt else "")
""", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class GuardTest(unittest.TestCase):
    def settings(self, status):
        s = Settings()
        s.llm_provider, s.codex_bin = "codex", fake_codex(status)
        return s

    @unittest.skipIf(os.name == "nt", "подставной codex — shell-скрипт")
    def test_chatgpt_login_accepted(self):
        import asyncio
        llm = make_llm(self.settings("Logged in using ChatGPT"))
        self.assertEqual(llm.engine_label, "codex (ChatGPT account)")
        self.assertIn("OK", asyncio.run(llm.complete("s", "u")))

    @unittest.skipIf(os.name == "nt", "подставной codex — shell-скрипт")
    def test_not_logged_in_refused(self):
        with self.assertRaises(LLMError):
            CodexChatGPTLLM(self.settings("Not logged in"))

    @unittest.skipIf(os.name == "nt", "подставной codex — shell-скрипт")
    def test_api_key_is_not_chatgpt_account(self):
        with self.assertRaises(LLMError):
            CodexChatGPTLLM(self.settings("Logged in using an API key - sk-***"))

    def test_missing_cli_refused(self):
        s = Settings()
        s.llm_provider, s.codex_bin = "codex", "/nonexistent/codex"
        with self.assertRaises(LLMError):
            make_llm(s)


if __name__ == "__main__":
    unittest.main()
