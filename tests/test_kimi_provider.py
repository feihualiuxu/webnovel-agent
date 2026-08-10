from __future__ import annotations

import unittest
from unittest.mock import patch

from novel_agent.cli import build_parser
from novel_agent.llm_client import LLMClient, openai_compatible_url
from novel_agent.ui import INDEX_HTML, PROVIDERS, key_env_for_provider


class KimiProviderTests(unittest.TestCase):
    def test_kimi_defaults_and_key_pool(self) -> None:
        client = LLMClient.from_values("kimi", api_keys=["test-key-one", "test-key-two"])

        self.assertEqual(client.config.model, "kimi-k3")
        self.assertEqual(client.config.base_url, "https://api.moonshot.cn/v1")
        self.assertEqual(client.config.api_keys, ["test-key-one", "test-key-two"])

    def test_kimi_request_uses_k3_parameters_and_normalized_url(self) -> None:
        client = LLMClient.from_values("kimi", api_key="test-key", base_url="https://api.moonshot.cn/v1")
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(client, "_post_json", return_value=response) as post_json:
            result = client.complete("写一段测试正文", max_tokens=2048, temperature=0.6)

        self.assertEqual(result, "ok")
        url, headers, body = post_json.call_args.args
        self.assertEqual(url, "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["model"], "kimi-k3")
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_cli_and_ui_expose_kimi(self) -> None:
        args = build_parser().parse_args(["test-llm", "--provider", "kimi"])

        self.assertEqual(args.provider, "kimi")
        self.assertIn("kimi", PROVIDERS)
        self.assertIn('<option value="kimi">Kimi K3 API</option>', INDEX_HTML)
        self.assertEqual(
            key_env_for_provider("kimi", {"api_keys": "first\nsecond"}),
            {"KIMI_API_KEYS": "first\nsecond", "KIMI_API_KEY": "first"},
        )

    def test_openai_compatible_url_accepts_both_base_styles(self) -> None:
        self.assertEqual(
            openai_compatible_url("https://api.moonshot.cn", "chat/completions"),
            "https://api.moonshot.cn/v1/chat/completions",
        )
        self.assertEqual(
            openai_compatible_url("https://api.moonshot.cn/v1", "/chat/completions"),
            "https://api.moonshot.cn/v1/chat/completions",
        )


if __name__ == "__main__":
    unittest.main()
