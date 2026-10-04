"""Parse user-provided Arbeitsagentur HTML without making network requests.

The parser accepts a saved search-results page or a saved job-detail page. It
prefers Schema.org ``JobPosting`` JSON-LD when present, then falls back to
common semantic HTML and visible text patterns. Site-specific fallbacks can be
refined when a representative HTML sample is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict

SOURCE_NAME = "Agentur für Arbeit"
BA_ORIGIN = "https://www.arbeitsagentur.de"
JOB_DETAIL_PATH = re.compile(r"/jobsuche/jobdetail/([^/?#]+)", re.IGNORECASE)


class JobPosting(BaseModel):
    """Normalized job fields extracted from a supplied HTML document."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    company: str | None = None
    source: str = SOURCE_NAME
    source_job_id: str | None = None
    job_url: str | None = None
    employer_job_url: str | None = None
    application_url: str | None = None
    job_description: str | None = None
    location: str | None = None
    work_mode: str | None = None
    employment_type: str | None = None
    employment_type_text: str | None = None
    posted_at: str | None = None
    posted_at_text: str | None = None

    @property
    def deduplication_key(self) -> str | None:
        source_key = (
            "arbeitsagentur" if self.source == SOURCE_NAME
            else re.sub(r"[^a-z0-9]+", "-", self.source.casefold()).strip("-")
        )
        if self.source_job_id:
            return f"{source_key}:{self.source_job_id}"
        if self.job_url:
            return f"{source_key}:{self.job_url.rstrip('/')}"
        return None

    def to_dict(self) -> dict[str, str | None]:
        """Return the normalized fields using Python-friendly names."""
        return self.model_dump()

    def to_airtable_fields(self) -> dict[str, str]:
        """Map populated values to the existing unified Jobs field names."""
        values: dict[str, str | None] = {
            "Title": self.title,
            "Company": self.company,
            "Source": self.source,
            "Source Job ID": self.source_job_id,
            "Job URL": self.job_url,
            "Deduplication Key": self.deduplication_key,
            "Application URL": self.employer_job_url or self.application_url,
            "Job Description": self.job_description,
            "Location": self.location,
            "Work Mode": self.work_mode,
            "Employment Type": self.employment_type,
            "Posted At": self.posted_at,
        }
        return {key: value for key, value in values.items() if value}


def parse_html(html: str, source_url: str | None = None) -> list[JobPosting]:
    """Extract jobs from one manually supplied HTML page.

    Search pages may contain multiple result cards. Detail pages usually
    produce one result. No URL is fetched by this function.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_url = _safe_source_url(source_url)

    structured = _parse_json_ld(soup, page_url)
    if structured:
        return _deduplicate(structured)

    cards = _parse_result_cards(soup, page_url)
    if cards:
        return _deduplicate(cards)

    detail = _parse_detail_page(soup, page_url)
    return [detail] if any((detail.title, detail.source_job_id, detail.job_description)) else []


def _safe_source_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "www.arbeitsagentur.de",
        "arbeitsagentur.de",
    }:
        return None
    return source_url


def _parse_json_ld(soup: BeautifulSoup, source_url: str | None) -> list[JobPosting]:
    results: list[JobPosting] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _walk_json(payload):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(str(kind).lower() == "jobposting" for kind in types):
                continue
            results.append(_from_json_ld(item, source_url))
    return results


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _from_json_ld(item: dict[str, Any], source_url: str | None) -> JobPosting:
    organization = item.get("hiringOrganization")
    if isinstance(organization, list):
        organization = organization[0] if organization else None
    company = organization.get("name") if isinstance(organization, dict) else organization

    location = _json_location(item.get("jobLocation"))
    date_value = item.get("datePosted")
    date_iso, date_text = _normalize_date(date_value)
    identifier = item.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")

    job_url = _absolute_url(item.get("url"), source_url) or source_url
    job_id = _job_id(job_url) or _clean(identifier)
    employment = item.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(part) for part in employment)
    work_mode = _schema_work_mode(item)

    return JobPosting(
        title=_normalize_title(_clean(item.get("title")), _clean(company)),
        company=_clean(company),
        source_job_id=job_id,
        job_url=_canonical_job_url(job_url, job_id),
        employer_job_url=_absolute_url(item.get("url"), source_url)
        if _is_external_url(_absolute_url(item.get("url"), source_url))
        else None,
        application_url=_absolute_url(item.get("directApply"), source_url),
        job_description=_html_to_text(item.get("description")),
        location=location,
        work_mode=work_mode,
        employment_type=_normalize_employment(_clean(employment)),
        employment_type_text=_clean(employment),
        posted_at=date_iso,
        posted_at_text=date_text,
    )


def _parse_result_cards(soup: BeautifulSoup, source_url: str | None) -> list[JobPosting]:
    postings: list[JobPosting] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=JOB_DETAIL_PATH):
        job_url = _absolute_url(anchor.get("href"), source_url)
        if not job_url or job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        card = _find_result_card(anchor)
        text_lines = _text_lines(card)
        company = _first_text(card, (".firma-lane", '[id$="-firma"]')) or _find_labeled_value(
            card, {"arbeitgeber", "unternehmen", "firma"}
        )
        title = _first_text(card, (".titel-lane", '[id$="-titel"]'))
        if title:
            title = re.sub(r"^\s*\d+[.):]?\s*", "", title)
            if company:
                title = re.sub(rf"^{re.escape(company)}\s*:\s*", "", title, flags=re.IGNORECASE)
        title = title or _first_heading(card) or _clean(anchor.get_text(" ", strip=True))
        if title and company:
            title = re.sub(rf"\s+bei\s+{re.escape(company)}\s*$", "", title, flags=re.IGNORECASE)
        date_text = _find_date_text(card)
        date_iso, normalized_date_text = _normalize_date(date_text)
        employment_text = _find_labeled_value(card, {"anstellungsart", "arbeitszeit", "beschäftigungsart"})
        fixed_term = _find_labeled_value(card, {"befristung", "vertragsdauer"})
        if fixed_term:
            employment_text = f"{employment_text or ''} {fixed_term}".strip()
        employment_text = employment_text or _find_employment_type(card, text_lines)

        postings.append(
            JobPosting(
                title=title,
                company=company or _candidate_line(text_lines, exclude={title, date_text}, location=False),
                source_job_id=_job_id(job_url),
                job_url=_canonical_job_url(job_url, _job_id(job_url)),
                employer_job_url=_find_employer_job_url(card, source_url),
                job_description=_find_description(card),
                location=_find_location(card, text_lines),
                work_mode=_find_work_mode(card),
                employment_type=_normalize_employment(employment_text),
                employment_type_text=employment_text,
                posted_at=date_iso,
                posted_at_text=normalized_date_text,
            )
        )
    return postings


def _parse_detail_page(soup: BeautifulSoup, source_url: str | None) -> JobPosting:
    page_title = _meta_content(soup, "og:title") or _first_heading(soup)
    title, company_from_title = _split_ba_heading(page_title)

    pairs = _label_value_pairs(soup)
    def labeled(*labels: str) -> str | None:
        wanted = {_fold(label) for label in labels}
        for key, value in pairs.items():
            if _fold(key).rstrip(":") in wanted:
                return value
        return _find_labeled_value(soup, set(labels))
        return None

    description = _find_description(soup)
    posted_text = labeled("Veröffentlichungsdatum", "Veröffentlicht", "Eingestellt am")
    if not posted_text:
        posted_text = _find_date_text(soup)
    posted_at, posted_text = _normalize_date(posted_text)

    job_id = _job_id(source_url)
    return JobPosting(
        title=title or _clean(page_title),
        company=labeled("Arbeitgeber", "Unternehmen", "Firma") or company_from_title,
        source_job_id=job_id,
        job_url=_canonical_job_url(source_url, job_id),
        employer_job_url=_find_employer_job_url(soup, source_url),
        application_url=_find_application_url(soup, source_url),
        job_description=description,
        location=labeled("Arbeitsort", "Arbeitsplatz", "Standort") or _find_location(soup, _text_lines(soup)),
        work_mode=_find_work_mode(soup),
        employment_type=_normalize_employment(
            labeled("Anstellungsart", "Arbeitszeit", "Beschäftigungsart", "Befristung", "Vertragsdauer")
            or _find_employment_type(soup, _text_lines(soup))
        ),
        employment_type_text=(
            labeled("Anstellungsart", "Arbeitszeit", "Beschäftigungsart", "Befristung", "Vertragsdauer")
            or _find_employment_type(soup, _text_lines(soup))
        ),
        posted_at=posted_at,
        posted_at_text=posted_text,
    )


def _find_result_card(anchor: Tag) -> Tag:
    article: Tag | None = None
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "li" and "listeneintrag" in parent.get("class", []):
            return parent
        if parent.name == "article":
            article = parent
        text = _clean(parent.get_text(" ", strip=True)) or ""
        if parent.name == "li" and len(text) >= 40:
            return parent
        attrs = " ".join([str(parent.get("class", "")), str(parent.get("id", ""))]).lower()
        if any(token in attrs for token in ("job", "result", "stelle", "angebot")) and 40 <= len(text) <= 6000:
            return parent
        if len(text) > 6000:
            break
    return article or (anchor.parent if isinstance(anchor.parent, Tag) else anchor)


def _label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for label in soup.find_all("dt"):
        value = label.find_next_sibling("dd")
        if value:
            key = _clean(label.get_text(" ", strip=True))
            text = _clean(value.get_text(" ", strip=True))
            if key and text:
                pairs[key] = text
    for label in soup.find_all("th"):
        value = label.find_next_sibling("td")
        if value:
            key = _clean(label.get_text(" ", strip=True))
            text = _clean(value.get_text(" ", strip=True))
            if key and text:
                pairs[key] = text
    for node in soup.find_all(string=re.compile(r"(?:Arbeitsort|Arbeitgeber|Veröffentlichungsdatum|Anstellungsart)\s*:", re.I)):
        parent = node.parent
        text = _clean(parent.get_text(" ", strip=True)) if isinstance(parent, Tag) else None
        if text:
            match = re.match(r"([^:]+):\s*(.+)", text)
            if match:
                pairs.setdefault(match.group(1), match.group(2))
    return pairs


def _find_labeled_value(container: Tag, labels: set[str]) -> str | None:
    for key, value in _label_value_pairs(container).items():
        if _fold(key).rstrip(":") in {_fold(label) for label in labels}:
            return value
    field_ids = {
        "arbeitsort": "arbeitsort",
        "arbeitsplatz": "arbeitsort",
        "standort": "arbeitsort",
        "arbeitszeit": "anstellungsart",
        "anstellungsart": "anstellungsart",
        "beschäftigungsart": "anstellungsart",
        "befristung": "befristung",
        "vertragsdauer": "befristung",
    }
    for label in labels:
        suffix = field_ids.get(_fold(label))
        node = container.select_one(f'[id$="-{suffix}"]') if suffix else None
        if node:
            values = [
                _clean(child.get_text(" ", strip=True))
                for child in node.find_all("span")
                if "sr-only" not in child.get("class", [])
            ]
            value = next((item for item in values if item), None)
            if value:
                return value
    return None


def _find_description(soup: BeautifulSoup) -> str | None:
    for selector in (
        '[itemprop="description"]',
        '[class*="stellenbeschreibung"]',
        '[id*="stellenbeschreibung"]',
        '[class*="job-description"]',
        '[class*="description"]',
    ):
        node = soup.select_one(selector)
        text = _clean(node.get_text("\n", strip=True)) if node else None
        if text and len(text) > 100:
            return text
    meta_description = _meta_content(soup, "description")
    if meta_description and len(meta_description) > 100:
        return meta_description
    return None


def _find_application_url(soup: BeautifulSoup, source_url: str | None) -> str | None:
    for anchor in soup.find_all("a", href=True):
        label = _fold(anchor.get_text(" ", strip=True))
        if any(word in label for word in ("bewerben", "zur bewerbung", "online application", "apply now")):
            return _absolute_url(anchor["href"], source_url)
    return None


def _find_employer_job_url(soup: BeautifulSoup, source_url: str | None) -> str | None:
    """Find a clearly identified outbound link to the employer's job posting."""
    cues = (
        "extern",
        "originalanzeige",
        "arbeitgeberseite",
        "stellenanzeige",
        "stellenangebot",
        "job posting",
        "bewerb",
        "zur website",
        "zur webseite",
        "company job",
    )
    candidates: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        url = _absolute_url(anchor.get("href"), source_url)
        if not _is_external_url(url):
            continue
        label = _fold(" ".join(
            str(value)
            for value in (
                anchor.get_text(" ", strip=True),
                anchor.get("aria-label", ""),
                anchor.get("title", ""),
            )
            if value
        ))
        matched_cues = [cue for cue in cues if cue in label]
        if matched_cues:
            candidates.append((len(matched_cues), url))
        elif anchor.get("target") == "_blank" and any(
            cue in label for cue in ("arbeitgeber", "company", "job", "stelle")
        ):
            candidates.append((1, url))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _is_external_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() not in {
        "www.arbeitsagentur.de",
        "arbeitsagentur.de",
    }


def _find_location(soup: BeautifulSoup, lines: list[str]) -> str | None:
    labeled = _find_labeled_value(soup, {"arbeitsort", "arbeitsplatz", "standort"})
    if labeled:
        return labeled
    for line in lines:
        if re.search(r"\b\d{5}\b", line) and len(line) < 180:
            return line
    return None


def _find_work_mode(soup: BeautifulSoup) -> str | None:
    text = _fold(soup.get_text(" ", strip=True))
    if re.search(r"\b(fully remote|100% remote|telearbeit)\b", text):
        return "Remote"
    if re.search(r"\bhybrid\b|homeoffice moeglich|home office moeglich|homeoffice|home office", text):
        return "Hybrid"
    if re.search(r"\bremote\b", text):
        return "Remote"
    return None


def _find_employment_type(soup: BeautifulSoup, lines: list[str]) -> str | None:
    labeled = _find_labeled_value(soup, {"arbeitszeit", "anstellungsart", "beschäftigungsart"})
    if labeled:
        return labeled
    for line in lines:
        folded = _fold(line)
        if any(token in folded for token in ("vollzeit", "teilzeit", "full-time", "part-time", "freiberuflich", "selbständigkeit")):
            return line
    return None


def _first_text(container: Tag, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = container.select_one(selector)
        if node:
            text = _clean(node.get_text(" ", strip=True))
            if text:
                return text
    return None


def _find_date_text(soup: BeautifulSoup) -> str | None:
    time = soup.find("time", attrs={"datetime": True})
    if time:
        return str(time.get("datetime"))
    ba_date = soup.select_one('[id$="-veroeffentlichungsdatum"]')
    if ba_date:
        title = _clean(ba_date.get("title"))
        if title:
            match = re.search(r"\d{1,2}[.]\d{1,2}[.]\d{4}|\d{4}-\d{2}-\d{2}", title)
            if match:
                return match.group(0)
    text = soup.get_text(" ", strip=True)
    match = re.search(
        r"(?:veröffentlicht|veröffentlichungsdatum|eingestellt(?:\s+am)?)\s*:?\s*("
        r"\d{1,2}[.]\d{1,2}[.]\d{4}|\d{4}-\d{2}-\d{2}|heute|gestern|(?:vor\s+)?\d+\s+(?:tag(?:en)?|woche(?:n)?|monat(?:en)?|stunde(?:n)?))",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _normalize_date(value: Any) -> tuple[str | None, str | None]:
    text = _clean(value)
    if not text:
        return None, None
    candidate = text[:10]
    try:
        parsed = date.fromisoformat(candidate)
        return parsed.isoformat(), text
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat(), text
        except ValueError:
            continue
    return None, text


def _json_location(value: Any) -> str | None:
    if isinstance(value, list):
        locations = [_json_location(item) for item in value]
        return "; ".join(part for part in locations if part) or None
    if not isinstance(value, dict):
        return _clean(value)
    address = value.get("address", value)
    if not isinstance(address, dict):
        return _clean(address)
    parts = [address.get(key) for key in ("addressLocality", "addressRegion", "postalCode", "addressCountry")]
    cleaned = [_clean(part) for part in parts]
    unique = list(dict.fromkeys(part for part in cleaned if part))
    return ", ".join(unique) or None


def _schema_work_mode(item: dict[str, Any]) -> str | None:
    remote_types = {"telecommute", "remote"}
    work_type = item.get("jobLocationType")
    values = work_type if isinstance(work_type, list) else [work_type]
    if any(_fold(value) in remote_types for value in values if value):
        return "Remote"
    return None


def _normalize_employment(value: str | None) -> str | None:
    if not value:
        return None
    folded = _fold(value)
    if any(token in folded for token in ("freelance", "freiberuflich")):
        return "Freelance"
    if any(token in folded for token in ("contract", "vertrag")) or (
        "befristet" in folded and "unbefristet" not in folded
    ):
        return "Contract"
    permanent = any(token in folded for token in ("permanent", "unbefristet"))
    if permanent and any(token in folded for token in ("part_time", "part-time", "teilzeit")):
        return "Permanent part-time"
    if permanent and any(token in folded for token in ("full_time", "full-time", "vollzeit")):
        return "Permanent full-time"
    return "Unknown"


def _split_ba_heading(value: str | None) -> tuple[str | None, str | None]:
    text = _clean(value)
    if not text:
        return None, None
    text = re.sub(r"^Stellenangebot\s*:\s*", "", text, flags=re.IGNORECASE)
    parts = re.split(r"\s+bei\s+", text, maxsplit=1, flags=re.IGNORECASE)
    return _clean(parts[0]), _clean(parts[1]) if len(parts) > 1 else None


def _normalize_title(title: str | None, company: str | None) -> str | None:
    if not title:
        return None
    if company:
        title = re.sub(rf"^{re.escape(company)}\s*:\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(rf"\s+bei\s+{re.escape(company)}\s*$", "", title, flags=re.IGNORECASE)
    return _clean(title)


def _candidate_line(lines: list[str], exclude: set[str | None], *, location: bool) -> str | None:
    for line in lines:
        if not line or line in exclude or len(line) > 160:
            continue
        folded = _fold(line)
        if any(word in folded for word in ("veröffentlicht", "bewerben", "mehr erfahren", "merken")):
            continue
        if location and (re.search(r"\b\d{5}\b", line) or any(x in folded for x in ("deutschland", "sachsen", "berlin"))):
            return line
        if not location and not re.search(r"\b\d{5}\b", line):
            return line
    return None


def _text_lines(tag: Tag) -> list[str]:
    return [line for line in (_clean(part) for part in tag.get_text("\n", strip=True).splitlines()) if line]


def _first_heading(soup: BeautifulSoup | Tag) -> str | None:
    heading = soup.find("h1") or soup.find("h2")
    return _clean(heading.get_text(" ", strip=True)) if heading else None


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return _clean(tag.get("content")) if tag else None


def _job_id(url: str | None) -> str | None:
    if not url:
        return None
    match = JOB_DETAIL_PATH.search(urlparse(url).path)
    return match.group(1) if match else None


def _canonical_job_url(url: str | None, job_id: str | None) -> str | None:
    if job_id:
        return f"{BA_ORIGIN}/jobsuche/jobdetail/{job_id}"
    return url


def _absolute_url(value: Any, source_url: str | None) -> str | None:
    if isinstance(value, bool):
        return None
    text = _clean(value)
    if not text:
        return None
    absolute = urljoin(source_url or BA_ORIGIN, text)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"www.arbeitsagentur.de", "arbeitsagentur.de"}:
        return absolute if parsed.scheme == "https" else None
    return absolute


def _html_to_text(value: Any) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return _clean(soup.get_text("\n", strip=True))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _fold(value: Any) -> str:
    return (_clean(value) or "").casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def _deduplicate(postings: list[JobPosting]) -> list[JobPosting]:
    unique: dict[str, JobPosting] = {}
    for posting in postings:
        key = posting.deduplication_key or posting.job_url or f"{posting.title}|{posting.company}"
        if key not in unique:
            unique[key] = posting
        else:
            _merge_posting(unique[key], posting)
    return list(unique.values())


def _merge_posting(target: JobPosting, other: JobPosting) -> None:
    for field in JobPosting.__dataclass_fields__:
        if field == "source":
            continue
        if getattr(target, field) is None and getattr(other, field) is not None:
            setattr(target, field, getattr(other, field))


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a manually saved Arbeitsagentur HTML page.")
    parser.add_argument("html_file", type=Path, help="Path to a local .html file supplied by the user")
    parser.add_argument("--source-url", help="Original BA page URL, used only to resolve relative links")
    parser.add_argument("--airtable-fields", action="store_true", help="Output fields mapped to the Jobs Airtable schema")
    args = parser.parse_args()

    html = args.html_file.read_text(encoding="utf-8")
    postings = parse_html(html, args.source_url)
    if args.airtable_fields:
        output = [posting.to_airtable_fields() for posting in postings]
    else:
        output = [posting.to_dict() for posting in postings]
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
