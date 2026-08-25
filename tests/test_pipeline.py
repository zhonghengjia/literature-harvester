from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.pipeline import DEFAULT_CONFIG, effective_contact_email  # noqa: E402


class PipelineTests(unittest.TestCase):
    def test_contact_email_prefers_private_general_environment_variable(self) -> None:
        config = {**DEFAULT_CONFIG, "general": {**DEFAULT_CONFIG["general"], "contact_email": "file@example.org"}}
        env = {
            "LITERATURE_HARVESTER_CONTACT_EMAIL": "private@example.org",
            "UNPAYWALL_EMAIL": "unpaywall@example.org",
        }
        self.assertEqual(effective_contact_email(config, env), "private@example.org")

    def test_unpaywall_email_remains_compatible_fallback(self) -> None:
        self.assertEqual(
            effective_contact_email(DEFAULT_CONFIG, {"UNPAYWALL_EMAIL": "fallback@example.org"}),
            "fallback@example.org",
        )


if __name__ == "__main__":
    unittest.main()
