from __future__ import annotations

import unittest
from unittest.mock import patch

from job_scraper.application.browser_agent import BrowserAction, execute, handle_cookies


class _Target:
    def __init__(self, events: list[str], index: int) -> None:
        self.events, self.index = events, index

    def click(self, **_kwargs) -> None:
        self.events.append(f"click:{self.index}")

    def fill(self, value: str, **_kwargs) -> None:
        self.events.append(f"fill:{self.index}:{value}")

    def select_option(self, *, value: str, **_kwargs) -> None:
        self.events.append(f"select:{self.index}:{value}")


class _Locator:
    def __init__(self, events: list[str], modal: bool = False) -> None:
        self.events = events
        self.modal = modal

    def filter(self, **_kwargs) -> "_Locator":
        return self

    def nth(self, index: int) -> _Target:
        return _Target(self.events, index)

    def count(self) -> int:
        return 1 if self.modal else 0


class _Page:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.modal = False

    def locator(self, _selector: str) -> _Locator:
        return _Locator(self.events, self.modal and "dialog" in _selector)


class BrowserAgentTest(unittest.TestCase):
    def test_action_rejects_stale_target_cookie_and_submit(self) -> None:
        page = _Page()
        observation = {"observation_id": "one", "controls": [
            {"target_id": "c0", "tag": "button", "type": "button", "label": "Continue", "disabled": False},
            {"target_id": "c1", "tag": "button", "type": "button", "label": "Reject cookies", "disabled": False,
             "cookie_kind": "reject_optional"},
            {"target_id": "c2", "tag": "button", "type": "submit", "label": "Send application", "disabled": False},
            {"target_id": "c3", "tag": "select", "type": "select-one", "label": "Country", "disabled": False,
             "options": [{"label": "Germany", "value": "DE"}]},
        ]}
        with self.assertRaisesRegex(ValueError, "changed"):
            execute(page, observation, BrowserAction(observation_id="old", kind="click", target_id="c0"))
        with self.assertRaisesRegex(ValueError, "cookie chrome"):
            execute(page, observation, BrowserAction(observation_id="one", kind="click", target_id="c1"))
        with self.assertRaisesRegex(ValueError, "Final submit"):
            execute(page, observation, BrowserAction(observation_id="one", kind="click", target_id="c2"))
        with self.assertRaisesRegex(ValueError, "Option"):
            execute(page, observation, BrowserAction(observation_id="one", kind="select", target_id="c3", value="FR"))
        execute(page, observation, BrowserAction(observation_id="one", kind="select", target_id="c3", value="DE"))
        self.assertEqual(page.events, ["select:3:DE"])

    def test_cookie_policy_rejects_then_accepts_only_if_dialog_remains(self) -> None:
        page = _Page()
        initial = {"controls": [{"target_id": "c0", "cookie_kind": "reject_optional"},
                                {"target_id": "c1", "cookie_kind": "accept_all"}]}
        clear = {"controls": []}
        with patch("job_scraper.application.browser_agent.observe", return_value=clear):
            result, action = handle_cookies(page, initial)
        self.assertEqual(action, "rejected_optional")
        self.assertIs(result, clear)
        self.assertEqual(page.events, ["click:0"])
        page.events.clear()
        page.modal = True
        blocked = {"controls": [{"target_id": "c1", "cookie_kind": "accept_all"}]}
        with patch("job_scraper.application.browser_agent.observe", side_effect=[blocked, clear]):
            result, action = handle_cookies(page, initial)
        self.assertEqual(action, "accepted_all_after_reject_blocked")
        self.assertEqual(page.events, ["click:0", "click:1"])
        page.events.clear()
        page.modal = False
        with patch("job_scraper.application.browser_agent.observe", return_value=blocked):
            _, action = handle_cookies(page, initial)
        self.assertEqual(action, "rejected_optional")
        self.assertEqual(page.events, ["click:0"])


if __name__ == "__main__":
    unittest.main()
