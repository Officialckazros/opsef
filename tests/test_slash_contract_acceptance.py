"""Registration and forwarding contracts for the Discord slash adapter."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import discord

from sefbot import config, slash


class SlashRegistrationAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = slash.setup(self.client, lambda *_args: None)
        self.commands = {command.name: command for command in self.tree.get_commands()}

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def test_command_surface_fits_discord_and_excludes_removed_rce_and_mp3(self) -> None:
        self.assertLessEqual(len(self.commands), 100)
        self.assertNotIn("eval", self.commands)
        self.assertNotIn("exec", self.commands)
        self.assertNotIn("mp3", self.commands)
        self.assertIn("privacy", self.commands)
        self.assertIn("act", self.commands)
        self.assertIn("language", self.commands)
        self.assertIn("lang", self.commands)

    def test_dead_groq_llama_ids_remap_to_gpt_oss(self) -> None:
        self.assertEqual(
            config.canonical_model("llama-3.3-70b-versatile"),
            "openai/gpt-oss-120b",
        )
        self.assertEqual(
            config.canonical_model("llama-3.1-8b-instant"),
            "openai/gpt-oss-20b",
        )
        self.assertEqual(config.canonical_model("groq"), "openai/gpt-oss-120b")
        self.assertIn("openai/gpt-oss-120b", config.MODEL_FALLBACKS)
        self.assertNotIn("llama-3.3-70b-versatile", config.MODEL_FALLBACKS)

    def test_model_picker_lists_every_live_groq_chat_model(self) -> None:
        command = self.commands["model"]
        parameters = {parameter.name: parameter for parameter in command.parameters}
        self.assertIn("choice", parameters)
        values = [choice.value for choice in parameters["choice"].choices]
        names = [choice.name for choice in parameters["choice"].choices]
        self.assertIn("inferx", values)
        self.assertIn("big", values)
        for model_id, label in config.GROQ_CHAT_MODELS:
            self.assertIn(model_id, values)
            self.assertIn(label, names)
        self.assertNotIn("llama-3.3-70b-versatile", values)
        self.assertLessEqual(len(values), 25)
        self.assertEqual(len(values), len(set(values)))

    def test_upload_commands_register_real_attachment_options(self) -> None:
        expected = {
            "kb": {"attachment"},
            "import": {"attachment"},
            "describe": {"image"},
            "read": {"attachment"},
            "chat": {"attachment"},
            "ask": {"attachment"},
            "assistant": {"attachment"},
        }
        for command_name, attachment_names in expected.items():
            with self.subTest(command=command_name):
                parameters = {
                    parameter.name: parameter
                    for parameter in self.commands[command_name].parameters
                }
                for name in attachment_names:
                    self.assertIn(name, parameters)
                    self.assertIs(parameters[name].type, discord.AppCommandOptionType.attachment)

    async def test_alias_callbacks_forward_once_to_the_original_command_callback(self) -> None:
        # alias, original, arguments supplied to alias, arguments expected by original
        cases = [
            ("models", "model", (None,), (None,)),
            ("google", "search", ("query",), ("query",)),
            ("infosec", "cybersec", ("topic",), ("topic",)),
            ("sec", "cybersec", ("topic",), ("topic",)),
            ("song", "music", ("song",), ("song",)),
            ("assist", "assistant", ("request", None), ("request", None)),
            ("level", "stats", (), ()),
            ("purge", "nuke", (17,), (17,)),
            ("quotes", "quote", ("random",), ("random",)),
            ("relationship", "rivalries", (), ()),
            ("lang", "language", (None, False), (None, False)),
        ]
        interaction = object()
        for alias_name, original_name, supplied, expected in cases:
            with self.subTest(alias=alias_name):
                original = self.commands[original_name]
                previous = original._callback
                forwarded = mock.AsyncMock()
                original._callback = forwarded
                try:
                    await self.commands[alias_name].callback(interaction, *supplied)
                finally:
                    original._callback = previous
                forwarded.assert_awaited_once_with(interaction, *expected)


if __name__ == "__main__":
    unittest.main()
