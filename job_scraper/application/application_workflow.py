"""Private, resumable application attempts shared by MCP and future ADK adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from job_scraper.application.jobs import JobNotFoundError
from job_scraper.models.jobs import JobMirrorRecord


class ApplicationWorkflowError(ValueError):
    """A user-actionable workflow precondition or state error."""


class ApplicationWorkflow:
    """Own attempt state, private artifacts, form inspection, and submit gates."""

    def __init__(self, repository: Any, project_root: Path, profiles: Any | None = None) -> None:
        self._repository = repository
        self._profiles = profiles
        configured_root = os.getenv("JOB_SCRAPER_APPLICATION_ARTIFACT_DIR")
        self._root = Path(configured_root).expanduser() if configured_root else project_root / ".local" / "application"
        if not self._root.is_absolute():
            self._root = project_root / self._root
        self._attempts = self._root / "attempts"
        self._facts_path = self._root / "answers.yaml"
        self._cv_map_path = self._root / "cv-map.json"

    def readiness(self, source_job_id: str) -> dict[str, Any]:
        job, classification = self._load_job_and_classification(source_job_id)
        cv_path = self._cv_path(classification["profile_id"])
        return {
            "source_job_id": source_job_id,
            "source": job.source,
            "deduplication_key": job.deduplication_key,
            "title": job.title,
            "company": job.company,
            "fit_status": job.fit_status,
            "classification_status": classification["status"],
            "profile_id": classification["profile_id"],
            "profile_version": classification["profile_version"],
            "classification_reason": classification["reason"],
            "application_url": job.application_url,
            "cv_ready": bool(cv_path and cv_path.is_file()),
            "cv_path": str(cv_path) if cv_path and cv_path.is_file() else None,
            "blocked_reason": None if cv_path and cv_path.is_file() else
                f"No prepared PDF is mapped for profile {classification['profile_id']}; configure {self._cv_map_path}.",
        }

    def approved_facts(self) -> str:
        """Return the private progressive fact sheet for the current task only."""
        if not self._facts_path.is_file():
            raise ApplicationWorkflowError("Private application facts file is missing.")
        return self._facts_path.read_text(encoding="utf-8")

    def add_approved_fact(self, key: str, value: str, explicit_user_approval: bool) -> dict[str, str]:
        """Append a new user-approved fact without replacing existing answers."""
        if not explicit_user_approval:
            raise ApplicationWorkflowError("Adding a reusable fact requires explicit user approval.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key):
            raise ApplicationWorkflowError("Fact key must be a lowercase identifier (2–64 characters).")
        if not value.strip() or len(value) > 2000:
            raise ApplicationWorkflowError("Fact value must contain 1–2000 characters.")
        content = self._facts_path.read_text(encoding="utf-8") if self._facts_path.exists() else "version: 1\nfacts:\n"
        if re.search(rf"^  {re.escape(key)}:\s*$", content, re.MULTILINE):
            raise ApplicationWorkflowError("That fact key already exists; review it before changing it.")
        entry = (
            f"  {key}:\n"
            f"    value: {json.dumps(value, ensure_ascii=False)}\n"
            "    status: approved\n"
            "    source: user_conversation\n"
            f"    approved_on: {datetime.now(timezone.utc).date().isoformat()}\n"
        )
        self._facts_path.parent.mkdir(parents=True, exist_ok=True)
        self._facts_path.write_text(content.rstrip() + "\n" + entry, encoding="utf-8")
        return {"key": key, "status": "approved", "message": "Added to the private progressive facts sheet."}

    def start_attempt(self, source_job_id: str) -> dict[str, Any]:
        if os.getenv("K_SERVICE"):
            raise ApplicationWorkflowError(
                "Cloud Run application attempts need persistent private artifact storage before they can start."
            )
        readiness = self.readiness(source_job_id)
        active = self._repository.get_application_attempt_for_job(
            readiness["source"], readiness["deduplication_key"]
        )
        if active:
            return self.get_attempt(active["id"])
        if readiness["blocked_reason"]:
            # Form discovery is still useful before the user provides CV files.
            readiness["warning"] = readiness.pop("blocked_reason")
        url = readiness["application_url"]
        if not url:
            raise ApplicationWorkflowError("This job has no application URL.")
        _validate_public_url(url)
        attempt_id = uuid4().hex
        directory = self._attempts / attempt_id
        directory.mkdir(parents=True, exist_ok=False)
        state = {
            "attempt_id": attempt_id,
            "source_job_id": source_job_id,
            "job": readiness,
            "application_url": url,
            "status": "Selected",
            "created_at": _now(),
            "updated_at": _now(),
            "fields": [],
            "answers": {},
            "review_digest": None,
        }
        try:
            self._repository.create_application_attempt({
                "id": attempt_id,
                "source": readiness["source"],
                "deduplication_key": readiness["deduplication_key"],
                "profile_id": readiness["profile_id"],
                "profile_version": readiness["profile_version"],
                "cv_variant": readiness["profile_id"],
                "artifact_ref": attempt_id,
                "created_at": state["created_at"],
            })
        except Exception:
            self._erase_attempt(attempt_id)
            concurrent = self._repository.get_application_attempt_for_job(
                readiness["source"], readiness["deduplication_key"]
            )
            if concurrent:
                return self.get_attempt(concurrent["id"])
            raise
        self._save(state)
        return self._public_state(state)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        state = self._load(attempt_id)
        try:
            state["job"] = self.readiness(state["source_job_id"])
        except (ApplicationWorkflowError, JobNotFoundError) as exc:
            state["job"]["warning"] = str(exc)
        return {
            key: state.get(key) for key in (
                "attempt_id", "source_job_id", "status", "job", "application_url",
                "form_title", "form_url", "fields", "answers", "review",
                "review_digest", "review_version", "approved_at", "created_at",
                "updated_at", "screenshot", "confirmation_url",
            )
        }

    def capture_path(self, attempt_id: str) -> Path:
        state = self._load(attempt_id)
        value = state.get("screenshot")
        if not value:
            raise ApplicationWorkflowError("Inspect the form before viewing its capture.")
        path = Path(value).resolve()
        directory = (self._attempts / attempt_id).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or path.suffix.lower() != ".png":
            raise ApplicationWorkflowError("Application capture is unavailable.")
        return path

    def inspect_form(self, attempt_id: str) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        state = self._load(attempt_id)
        self._require_open(state)
        _validate_public_url(state["application_url"])
        directory = self._attempts / attempt_id
        screenshot = directory / "form.png"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", _guard_browser_request)
                response = page.goto(state["application_url"], wait_until="domcontentloaded", timeout=30000)
                if response and response.status >= 400:
                    raise ApplicationWorkflowError(f"Application page returned HTTP {response.status}.")
                page.screenshot(path=str(screenshot), full_page=True)
                fields = _inspect_fields(page)
                title = page.title()
                final_url = page.url
            finally:
                browser.close()
        state.update({"form_title": title, "form_url": final_url,
                      "fields": fields, "inspected_at": _now(), "screenshot": str(screenshot)})
        self._transition(state, "Inspecting")
        return {"attempt_id": attempt_id, "status": state["status"], "form_title": title,
                "form_url": final_url, "fields": fields, "screenshot_path": str(screenshot),
                "notice": "Inspection reads the current page only; it does not advance multi-step forms."}

    def save_answers(
        self,
        attempt_id: str,
        answers: dict[str, str],
        explicitly_approved_consent_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        state = self._load(attempt_id)
        self._require_open(state)
        if not state["fields"]:
            raise ApplicationWorkflowError("Inspect the form before saving answers.")
        selectors = {field.get("selector"): field for field in state["fields"] if field.get("selector")}
        unknown = set(answers) - selectors.keys()
        if unknown:
            raise ApplicationWorkflowError("Answers must use selectors returned by inspect_application_form.")
        normalized = {key: value for key, value in answers.items() if isinstance(value, str)}
        approved_consent = set(explicitly_approved_consent_fields or [])
        consent_fields = {
            selector for selector, field in selectors.items()
            if re.search(r"talent\s*pool|share|connected companies|group|consent|retention|data processing", field["label"], re.I)
        }
        selecting_consent = {
            selector for selector in consent_fields
            if normalized.get(selector, "").casefold() not in {
                "", "false", "no", "0", "unchecked", "nein", "off", "decline"
            }
        }
        if selecting_consent - approved_consent:
            raise ApplicationWorkflowError(
                "Selecting talent-pool or group-sharing consent requires explicit user approval for this employer."
            )
        if approved_consent - consent_fields:
            raise ApplicationWorkflowError("Consent approvals must reference consent fields from the inspected form.")
        state["answers"] = normalized
        state["approved_consent_fields"] = sorted(approved_consent)
        state["updated_at"] = _now()
        state["review_digest"] = None
        self._transition(state, "Draft")
        return {"attempt_id": attempt_id, "status": state["status"],
                "saved_fields": len(normalized), "note": "Answers are private temporary attempt data."}

    def fill_for_review(self, attempt_id: str) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        state = self._load(attempt_id)
        self._require_open(state)
        if not state["fields"]:
            raise ApplicationWorkflowError("Inspect the form before preparing a review.")
        readiness = self.readiness(state["source_job_id"])
        self._check_selected_profile(attempt_id, readiness)
        if not readiness["cv_ready"]:
            raise ApplicationWorkflowError(readiness["blocked_reason"])
        cv_path = Path(readiness["cv_path"])
        if cv_path.suffix.lower() != ".pdf":
            raise ApplicationWorkflowError("Configured CV must be a compiled PDF.")
        fields = {field.get("selector"): field for field in state["fields"] if field.get("selector")}
        missing = [field["label"] for selector, field in fields.items()
                   if field["required"] and field["type"] not in {"file", "radio"}
                   and (not state["answers"].get(selector, "").strip()
                        or (field["type"] == "checkbox" and state["answers"][selector].casefold()
                            not in {"true", "yes", "1", "checked"}))]
        required_radio_groups = {
            field.get("name") or selector for selector, field in fields.items()
            if field["required"] and field["type"] == "radio"
        }
        for group in required_radio_groups:
            if not any(
                state["answers"].get(selector, "").casefold() in {"true", "yes", "1", "checked"}
                for selector, field in fields.items()
                if field["type"] == "radio" and (field.get("name") or selector) == group
            ):
                missing.append(group)
        if missing:
            raise ApplicationWorkflowError("Required answers are missing: " + ", ".join(missing))
        directory = self._attempts / attempt_id
        screenshot = directory / "review.png"
        review: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", _guard_browser_request)
                page.goto(state["application_url"], wait_until="domcontentloaded", timeout=30000)
                if page.url != state.get("form_url") or _inspect_fields(page) != state["fields"]:
                    raise ApplicationWorkflowError("Employer form changed since inspection; inspect it again before reviewing.")
                for selector, value in state["answers"].items():
                    field = fields[selector]
                    locator = page.locator(selector)
                    if field["tag"] == "select":
                        locator.select_option(value=value)
                    elif field["type"] == "radio":
                        if value.casefold() in {"true", "yes", "1", "checked"}:
                            locator.check()
                    elif field["type"] == "checkbox":
                        if value.casefold() in {"true", "yes", "1", "checked"}:
                            locator.check()
                        elif value.casefold() in {"false", "no", "0", "unchecked"}:
                            locator.uncheck()
                        else:
                            locator.check(value=value)
                    else:
                        locator.fill(value)
                    review.append({"question": field["label"], "answer": value})
                file_inputs = page.locator('input[type="file"]')
                if not file_inputs.count():
                    raise ApplicationWorkflowError("No CV upload field was found on the current form page.")
                file_inputs.first.set_input_files(str(cv_path))
                page.screenshot(path=str(screenshot), full_page=True)
                title = page.title()
                final_url = page.url
            finally:
                browser.close()
        review.append({"question": "CV upload", "answer": cv_path.name})
        cv_hash = hashlib.sha256(cv_path.read_bytes()).hexdigest()
        digest = hashlib.sha256(json.dumps({
            "answers": review, "cv_sha256": cv_hash, "form_url": final_url,
            "fields": state["fields"],
        }, sort_keys=True).encode()).hexdigest()
        state.update({"review": review, "review_digest": digest,
                      "review_cv_path": str(cv_path), "review_cv_sha256": cv_hash,
                      "screenshot": str(screenshot), "form_title": title, "form_url": final_url,
                      "updated_at": _now()})
        self._transition(
            state, "ReadyForReview", review_digest=digest,
            cv_variant=readiness["profile_id"], increment_review=True,
        )
        return {"attempt_id": attempt_id, "status": state["status"], "review_digest": digest,
                "form_title": title, "form_url": final_url, "answers": review,
                "screenshot_path": str(screenshot), "cv_variant": readiness["profile_id"],
                "cv_filename": cv_path.name,
                "consent_note": "Review employer-specific consent explicitly; consent is never inferred from facts."}

    def submit(self, attempt_id: str, review_digest: str, explicit_user_approval: bool) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        state = self._load(attempt_id)
        if state["status"] != "ReadyForReview" or not state.get("review_digest"):
            raise ApplicationWorkflowError("This attempt has no completed review ready to submit.")
        if not explicit_user_approval or review_digest != state["review_digest"]:
            raise ApplicationWorkflowError("Submission requires explicit user approval of this exact review digest.")
        readiness = self.readiness(state["source_job_id"])
        self._check_selected_profile(attempt_id, readiness)
        if not readiness["cv_ready"]:
            raise ApplicationWorkflowError(readiness["blocked_reason"])
        cv_path = Path(readiness["cv_path"])
        if str(cv_path) != state.get("review_cv_path") or hashlib.sha256(cv_path.read_bytes()).hexdigest() != state.get("review_cv_sha256"):
            raise ApplicationWorkflowError("Prepared CV changed after review; prepare a new review before submitting.")
        fields = {field.get("selector"): field for field in state["fields"] if field.get("selector")}
        metadata = self._repository.get_application_attempt(attempt_id)
        if metadata is None or metadata["review_digest"] != review_digest:
            raise ApplicationWorkflowError("The persisted review changed; prepare it again before submitting.")
        self._transition(
            state, "Submitting", approved_at=_now(), submit_started_at=_now(),
            expected_review_digest=review_digest,
        )
        clicked = False
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.route("**/*", _guard_browser_request)
                    page.goto(state["application_url"], wait_until="domcontentloaded", timeout=30000)
                    if page.url != state.get("form_url") or _inspect_fields(page) != state["fields"]:
                        raise ApplicationWorkflowError("Employer form changed after review; inspect it again before submitting.")
                    for selector, value in state["answers"].items():
                        field = fields[selector]
                        locator = page.locator(selector)
                        if field["tag"] == "select":
                            locator.select_option(value=value)
                        elif field["type"] == "radio":
                            if value.casefold() in {"true", "yes", "1", "checked"}:
                                locator.check()
                        elif field["type"] == "checkbox":
                            locator.check() if value.casefold() in {"true", "yes", "1", "checked"} else locator.uncheck()
                        else:
                            locator.fill(value)
                    file_inputs = page.locator('input[type="file"]')
                    if not file_inputs.count():
                        raise ApplicationWorkflowError("No CV upload field was found on the current form page.")
                    file_inputs.first.set_input_files(readiness["cv_path"])
                    submit = page.locator('button[type="submit"], input[type="submit"]').last
                    submit_count = page.locator('button[type="submit"], input[type="submit"]').count()
                    if submit_count != 1:
                        raise ApplicationWorkflowError("Expected exactly one submit button; inspect the form manually.")
                    clicked = True
                    submit.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    confirmation = page.locator("body").inner_text(timeout=5000)[:2000]
                    page_url = page.url
                finally:
                    browser.close()
        except Exception as exc:
            next_status = "SubmissionUnverified" if clicked else "ReadyForReview"
            state["submission_error_type"] = type(exc).__name__
            if state["status"] != next_status:
                self._transition(state, next_status)
            if clicked:
                raise ApplicationWorkflowError(
                    "The submit action may have reached the employer, but confirmation was not verified. "
                    "Attempt data was retained; check the employer result before retrying."
                ) from exc
            raise
        if not re.search(r"thank you|application (was )?received|successfully submitted|bewerbung.*(eingegangen|erfolgreich)", confirmation, re.I | re.S):
            state.update({"confirmation_url": page_url,
                          "confirmation_excerpt": confirmation, "updated_at": _now()})
            self._transition(state, "SubmissionUnverified")
            return {"attempt_id": attempt_id, "status": state["status"], "confirmation_url": page_url,
                    "confirmation_text": confirmation, "temporary_attempt_erased": False,
                    "next_step": "Check the employer page and explicitly confirm submitted or discard; do not retry yet."}
        self._close_as_submitted(attempt_id)
        return {"attempt_id": attempt_id, "status": "Submitted", "confirmation_url": page_url,
                "confirmation_text": confirmation, "temporary_attempt_erased": True}

    def confirm_submitted(self, attempt_id: str, explicit_user_confirmation: bool) -> dict[str, Any]:
        if not explicit_user_confirmation:
            raise ApplicationWorkflowError("Closing as submitted requires explicit user confirmation.")
        metadata = self._repository.get_application_attempt(attempt_id)
        if metadata is None or metadata["status"] not in {"SubmissionUnverified", "Submitting"}:
            raise ApplicationWorkflowError("Only an unverified submission can be confirmed manually.")
        self._close_as_submitted(attempt_id)
        return {"attempt_id": attempt_id, "status": "Submitted", "temporary_attempt_erased": True}

    def discard(self, attempt_id: str, explicit_user_discard: bool) -> dict[str, Any]:
        if not explicit_user_discard:
            raise ApplicationWorkflowError("Discard requires an explicit user decision.")
        if self._repository.get_application_attempt(attempt_id) is None:
            raise ApplicationWorkflowError("Application attempt was not found or already closed.")
        self._erase_attempt(attempt_id)
        self._repository.delete_application_attempt(attempt_id)
        return {"attempt_id": attempt_id, "status": "Discarded", "temporary_attempt_erased": True}

    def _load_job_and_classification(self, source_job_id: str) -> tuple[JobMirrorRecord, dict[str, Any]]:
        row = self._repository.get_job(source_job_id)
        if row is None:
            raise JobNotFoundError(source_job_id)
        job = JobMirrorRecord.model_validate(row)
        if job.fit_status != "Scored":
            raise ApplicationWorkflowError("Only scored job rows can start an application attempt.")
        if job.application_status != "Saved":
            raise ApplicationWorkflowError("This job is not in Saved status; check its application history before continuing.")
        if not job.source_job_id:
            raise ApplicationWorkflowError("This tracker row has no source job ID.")
        classification = self._repository.get_classification(job.source, job.deduplication_key)
        if not classification or classification["status"] != "Classified" or not classification.get("profile_id"):
            raise ApplicationWorkflowError("Saved classification is missing or unclear; pause and resolve it first.")
        if self._profiles is not None:
            try:
                current = self._profiles.get(classification["profile_id"])
            except Exception as exc:
                raise ApplicationWorkflowError("Saved classification references an unavailable profile.") from exc
            if current.status == "draft" or current.version != classification.get("profile_version"):
                raise ApplicationWorkflowError("Saved classification is stale or not reviewed; pause and reclassify.")
        return job, classification

    def _check_selected_profile(self, attempt_id: str, readiness: dict[str, Any]) -> None:
        metadata = self._repository.get_application_attempt(attempt_id)
        if metadata is None or (metadata["profile_id"], metadata["profile_version"]) != (
            readiness["profile_id"], readiness["profile_version"]
        ):
            raise ApplicationWorkflowError("Job classification changed; review the selected CV before continuing.")

    def _cv_path(self, profile_id: str) -> Path | None:
        if not self._cv_map_path.exists():
            return None
        mapping = json.loads(self._cv_map_path.read_text(encoding="utf-8"))
        value = mapping.get("profiles", {}).get(profile_id)
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else self._root / path

    def _load(self, attempt_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise ApplicationWorkflowError("Invalid application attempt ID.")
        metadata = self._repository.get_application_attempt(attempt_id)
        if metadata is None:
            raise ApplicationWorkflowError("Application attempt was not found or was already closed.")
        path = self._attempts / attempt_id / "attempt.json"
        if not path.is_file():
            raise ApplicationWorkflowError("Attempt metadata exists, but its private artifacts are unavailable.")
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update({
            "status": metadata["status"],
            "review_version": metadata["review_version"],
            "review_digest": metadata["review_digest"],
            "approved_at": metadata["approved_at"],
            "updated_at": metadata["updated_at"],
        })
        return state

    def _save(self, state: dict[str, Any]) -> None:
        directory = self._attempts / state["attempt_id"]
        directory.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        (directory / "attempt.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _transition(self, state: dict[str, Any], new_status: str, **updates: Any) -> None:
        try:
            metadata = self._repository.transition_application_attempt(
                state["attempt_id"], state["status"], new_status, **updates
            )
        except ValueError as exc:
            raise ApplicationWorkflowError(str(exc)) from exc
        state.update({
            "status": metadata["status"],
            "review_version": metadata["review_version"],
            "review_digest": metadata["review_digest"],
            "approved_at": metadata["approved_at"],
        })
        self._save(state)

    def _public_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {key: state[key] for key in ("attempt_id", "source_job_id", "status", "application_url", "job")}

    def _require_open(self, state: dict[str, Any]) -> None:
        if state["status"] in {"Submitting", "SubmissionUnverified", "Submitted", "Discarded"}:
            raise ApplicationWorkflowError(
                f"Attempt is {state['status']}; confirm the employer result or discard it before editing."
            )

    def _erase_attempt(self, attempt_id: str) -> None:
        directory = self._attempts / attempt_id
        if not directory.resolve().is_relative_to(self._attempts.resolve()):
            raise ApplicationWorkflowError("Invalid attempt path.")
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=False)

    def _close_as_submitted(self, attempt_id: str) -> None:
        self._erase_attempt(attempt_id)
        self._repository.complete_application_attempt(attempt_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inspect_fields(page: Any) -> list[dict[str, Any]]:
    fields = page.locator("input:not([type=hidden]), select, textarea").evaluate_all(
        """els => els.map((el, i) => {
          const labels = [...(el.labels || [])].map(x => x.innerText.trim()).filter(Boolean);
          const labelled = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
          const id = el.id || '';
          return {tag:el.tagName.toLowerCase(), type:(el.type || '').toLowerCase(),
            name:el.name || '', id, label:labels.join(' / ') || labelled || id || el.name || `Field ${i+1}`,
            required:!!el.required, options:el.tagName==='SELECT' ? [...el.options].map(o=>({label:o.label || o.text, value:o.value})) : [],
            selector: id ? '#' + CSS.escape(id) : (el.name ?
              `${el.tagName.toLowerCase()}${el.type==='radio' ? '[type=radio]' : ''}[name="${CSS.escape(el.name)}"]${el.type==='radio' ? '[value="'+CSS.escape(el.value)+'"]' : ''}` : null)};
        })"""
    )
    return fields


def _validate_public_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApplicationWorkflowError("Application URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ApplicationWorkflowError("Application URL is not allowed.")


def _guard_browser_request(route: Any) -> None:
    """Prevent application pages and redirects from reaching private network hosts."""
    target = urlsplit(route.request.url)
    if target.scheme not in {"http", "https"} or not target.hostname:
        route.abort()
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(target.hostname, target.port or 443)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            route.abort()
            return
    except (OSError, ValueError):
        route.abort()
        return
    route.continue_()
