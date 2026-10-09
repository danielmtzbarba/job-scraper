"""Bounded Playwright actuator and compact observations for application pages."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class BrowserAction(BaseModel):
    observation_id: str
    kind: Literal["click", "fill", "select", "scroll", "pause"]
    target_id: str | None = None
    value: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def valid_shape(self) -> "BrowserAction":
        if self.kind in {"click", "fill", "select"} and not self.target_id:
            raise ValueError("A target is required")
        if self.kind in {"fill", "select"} and self.value is None:
            raise ValueError("A value is required")
        if self.kind == "scroll" and self.value not in {"up", "down"}:
            raise ValueError("Scroll direction must be up or down")
        return self


class ApplicationQuestion(BaseModel):
    target_id: str
    question: str = Field(max_length=300)
    required: bool
    answer: str | None = Field(default=None, max_length=2000)
    provenance: str | None = Field(default=None, max_length=150)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"


class AgentDecision(BaseModel):
    page_kind: Literal["application", "listing", "login", "confirmation", "unknown"]
    questions: list[ApplicationQuestion] = Field(default_factory=list, max_length=50)
    action: BrowserAction
    reason: str = Field(max_length=500)
    ask_user: str | None = Field(default=None, max_length=500)


_COOKIE_WORDS = re.compile(r"cookie|cookies|consent settings|datenschutz.einstellungen|einwilligung verwalten", re.I)
_REJECT_WORDS = re.compile(r"reject|decline|necessary only|essential only|only necessary|alle ablehnen|nur notwendige|nicht zustimmen", re.I)
_ACCEPT_WORDS = re.compile(r"accept all|allow all|alle akzeptieren|allen zustimmen", re.I)
_SUBMIT_WORDS = re.compile(r"submit|send application|complete application|finish application|bewerbung absenden|abschicken|jetzt bewerben", re.I)
EMPLOYER_CONSENT_WORDS = re.compile(
    r"talent\s*pool|share|connected companies|group|consent|retention|data processing|privacy|datenschutz|speicherung|weitere unternehmen|terms", re.I
)


def cookie_kind(control: dict[str, Any]) -> str | None:
    label = str(control.get("label") or "")
    context = str(control.get("context") or "")
    if not (_COOKIE_WORDS.search(context) or _COOKIE_WORDS.search(label)):
        return None
    if _REJECT_WORDS.search(label):
        return "reject_optional"
    if _ACCEPT_WORDS.search(label):
        return "accept_all"
    return "cookie_chrome"


def observe(page: Any) -> dict[str, Any]:
    """Return visible controls by stable index for this observation only."""
    controls = page.locator("input, select, textarea, button, a, [role=button]").evaluate_all(
        """els => els.filter(el => {
          const box=el.getBoundingClientRect(), style=getComputedStyle(el);
          return box.width>0 && box.height>0 && style.visibility!=='hidden' && style.display!=='none';
        }).slice(0,120).map((el,i) => {
          const label=[...(el.labels||[])].map(x=>x.innerText.trim()).filter(Boolean).join(' / ')
            || el.getAttribute('aria-label') || el.innerText?.trim() || el.getAttribute('placeholder')
            || el.getAttribute('value') || el.name || el.id || `Control ${i+1}`;
          const container=el.closest('[role=dialog], dialog, form, section') || el.parentElement;
          return {target_id:`c${i}`,tag:el.tagName.toLowerCase(),type:(el.type||'').toLowerCase(),
            label:label.slice(0,300),context:(container?.innerText||'').slice(0,350),
            required:!!el.required,disabled:!!el.disabled,checked:!!el.checked,
            value:(el.value||'').slice(0,2000),filled:!!el.value,
            options:el.tagName==='SELECT'?[...el.options].map(o=>({label:o.label,value:o.value})).slice(0,50):[]};
        })"""
    )
    for control in controls:
        control["cookie_kind"] = cookie_kind(control)
    body = page.locator("body").inner_text(timeout=5000)[:6000]
    public = {"url": page.url, "title": page.title(), "text": body,
              "controls": [{k: v for k, v in c.items() if k not in {"context", "value"}} for c in controls]}
    public["observation_id"] = hashlib.sha256(json.dumps({"page": public,
        "control_values": [c["value"] for c in controls]}, sort_keys=True).encode()).hexdigest()
    return public


def execute(page: Any, observation: dict[str, Any], action: BrowserAction) -> None:
    if action.observation_id != observation["observation_id"]:
        raise ValueError("Page observation changed; observe again")
    if action.kind == "pause":
        return
    if action.kind == "scroll":
        page.mouse.wheel(0, 500 if action.value == "down" else -500)
        return
    controls = {c["target_id"]: c for c in observation["controls"]}
    control = controls.get(action.target_id)
    if control is None or control["disabled"] or control.get("cookie_kind"):
        raise ValueError("Target is unavailable or is cookie chrome")
    if control["tag"] == "a" and action.kind == "click":
        raise ValueError("Navigation links need user review")
    if action.kind == "click":
        if _SUBMIT_WORDS.search(control["label"]) or control["type"] == "submit":
            raise ValueError("Final submit is available only through the approved review gate")
        if control["tag"] not in {"button", "input"} and control["type"] not in {"checkbox", "radio"}:
            raise ValueError("Target is not a clickable form control")
    elif action.kind == "fill" and control["tag"] not in {"input", "textarea"}:
        raise ValueError("Target cannot be filled")
    elif action.kind == "select":
        if control["tag"] != "select" or action.value not in {o["value"] for o in control["options"]}:
            raise ValueError("Option is not in the current observation")
    # Index matches the visible-control inventory generated by observe().
    locator = page.locator("input, select, textarea, button, a, [role=button]").filter(visible=True).nth(int(action.target_id[1:]))
    if action.kind == "click":
        locator.click(timeout=5000)
    elif action.kind == "fill":
        locator.fill(action.value, timeout=5000)
    else:
        locator.select_option(value=action.value, timeout=5000)


def handle_cookies(page: Any, observation: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Reject optional cookies first; accept all only if the blocking dialog survives."""
    controls = observation["controls"]
    reject = next((c for c in controls if c.get("cookie_kind") == "reject_optional"), None)
    accept = next((c for c in controls if c.get("cookie_kind") == "accept_all"), None)
    if not reject:
        return observation, None
    locator = page.locator("input, select, textarea, button, a, [role=button]").filter(visible=True)
    locator.nth(int(reject["target_id"][1:])).click(timeout=5000)
    next_observation = observe(page)
    # A surviving visible accept button inside cookie chrome is evidence that the dialog blocks progress.
    still_blocked = next((c for c in next_observation["controls"] if c.get("cookie_kind") == "accept_all"), None)
    modal_visible = page.locator('[role="dialog"][aria-modal="true"], dialog[open], [aria-modal="true"]').filter(visible=True).count() > 0
    if still_blocked and modal_visible:
        locator = page.locator("input, select, textarea, button, a, [role=button]").filter(visible=True)
        locator.nth(int(still_blocked["target_id"][1:])).click(timeout=5000)
        return observe(page), "accepted_all_after_reject_blocked"
    return next_observation, "rejected_optional"
