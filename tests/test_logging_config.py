"""Regression checks for the shared log redactor."""

import unittest

from job_scraper.logging_config import _scrub_sensitive_fields


class LogRedactionTests(unittest.TestCase):
    def test_token_counts_remain_visible_without_exposing_credentials(self) -> None:
        fields = {
            "prompt_tokens": 123,
            "candidate_tokens": 45,
            "thought_tokens": None,
            "access_token": "private-value",
            "api_key": "private-value",
            "prompt_tokens_extra": "private-value",
        }

        scrubbed = _scrub_sensitive_fields(None, "info", fields)

        self.assertEqual(scrubbed["prompt_tokens"], 123)
        self.assertEqual(scrubbed["candidate_tokens"], 45)
        self.assertIsNone(scrubbed["thought_tokens"])
        self.assertEqual(scrubbed["access_token"], "[REDACTED]")
        self.assertEqual(scrubbed["api_key"], "[REDACTED]")
        self.assertEqual(scrubbed["prompt_tokens_extra"], "[REDACTED]")

    def test_non_numeric_value_in_token_count_field_is_redacted(self) -> None:
        fields = {"prompt_tokens": "private-value"}

        self.assertEqual(
            _scrub_sensitive_fields(None, "info", fields)["prompt_tokens"],
            "[REDACTED]",
        )
