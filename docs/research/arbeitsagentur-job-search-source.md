# Bundesagentur für Arbeit Jobsuche: source and Actor feasibility

Research date: 2026-09-28

## Recommendation

Do **not** build or run an Apify Actor that collects BA Jobsuche results yet. The BA's current [portal terms](https://www.arbeitsagentur.de/nutzungsbedingungen) expressly say users must not employ robots, web spiders, or similar technologies, and must not use existing communications/programming interfaces contrary to the BA's intended purpose to read portal content for data collection and analysis (section 2a(3)). The terms also allow the BA to disable access for misuse (section 4). This directly conflicts with the proposed automated job-collection Actor. Ask BA for an authorized access path or written permission before implementation or live requests.

This research is limited to official BA public pages and official Apify docs. I did not run a search query against the service, access a backend API, scrape results, create/deploy an Actor, or send writes.

## What is officially reachable and documented

The BA publishes its user-facing Jobsuche at <https://www.arbeitsagentur.de/jobsuche/>. Its official search results page is addressable at `/jobsuche/suche`; the BA page itself exposes search controls for job type, free-text `Was`, location `Wo`, radius, filters, and sorting. Search results render listing cards with title, employer, location, employment time model, sometimes contract duration, posted-date wording, and optional indicators (e.g. home office). A result links to a BA detail page at `/jobsuche/jobdetail/{reference-id}`. The observed official detail page displays the title, employer, work location, offer type, work time, contract duration, start date, occupation, publication date, modification date, and job description. See the official [Jobsuche results page](https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=jobs) and an official [job detail example](https://www.arbeitsagentur.de/jobsuche/jobdetail/19143-0066856655-S).

The UI exposes sort choices for relevance, newest publication, last modified, and start date. Its results are rendered in a “Weitere Ergebnisse” (more results) interaction. The public BA documentation reviewed does **not** specify a stable pagination parameter, page size, exact query parameter contract for all filters, or response schema for a machine API. The current HTML/web search view can therefore establish visible behavior, but not a stable scraping contract.

### Exact official page routes observed

| Purpose | Official route | Contract supported by official source |
| --- | --- | --- |
| Jobsuche start page | `GET https://www.arbeitsagentur.de/jobsuche/` | Human-facing search UI; text fields for occupation/keyword/reference number, location, and radius. |
| Search/results page | `GET https://www.arbeitsagentur.de/jobsuche/suche?...` | HTML result page. The official site links/pages demonstrate query parameters such as `suchbereich=jobs`, `was`, `wo`, `angebotsart`, and `sort`; these examples are UI URLs, not a documented API specification. |
| Job detail page | `GET https://www.arbeitsagentur.de/jobsuche/jobdetail/{reference-id}` | Human-facing detail page; observed IDs look like `10001-...-S`. Its visible content includes job description and identifying/reference data. |

The BA site uses dynamically rendered search UI; the public pages reviewed do not publish the underlying JSON endpoint, request headers, authentication scheme, pagination protocol, rate limits, or machine-readable search/detail schema. Do not treat reverse-engineered or third-party-described internal endpoints as an official BA API contract.

## Access, terms, and limits

- The public portal is free to use for people, but the current [BA terms](https://www.arbeitsagentur.de/nutzungsbedingungen) prohibit robot/web-spider use and contrary-purpose API/interface use to collect/analyze portal content (section 2a(3)). This is stronger than merely an undocumented rate limit: automated scraping is expressly disallowed by the published terms.
- The same terms say BA may suspend/deactivate portal access in cases of misuse or terms violations (section 4).
- The terms mention an HR-BA-XML interface for employers to transmit their own job listings, not an API for third-party bulk retrieval of job listings. See BA's [HR-BA-XML interface information](https://www.arbeitsagentur.de/unternehmen/arbeitskraefte/hr-ba-xml-schnittstelle). This is not a suitable collection API for this project.
- No official BA Jobsuche machine API documentation, API terms for public job-search retrieval, authentication contract, quota/rate limit, or pagination specification was found in the official BA materials reviewed. Consequently, do not assume that an exposed/observed browser backend is sanctioned for Actor data collection.

## Apify implications (official Apify docs)

Apify Actors accept validated JSON input through an Actor input schema and can publish structured rows as dataset items. Official docs: [Actor input schema](https://docs.apify.com/actors/development/actor-definition/input-schema), [input schema specification](https://docs.apify.com/actors/development/actor-definition/input-schema/specification/v1), [Actorization design guidance](https://docs.apify.com/academy/actorization), and [run Actor/retrieve dataset data](https://docs.apify.com/academy/api/run-actor-and-retrieve-data-via-api).

These docs explain how to package an Actor; they do not authorize the Actor's target-site collection. Apify's current [shared-responsibility model](https://docs.apify.com/security/shared-responsibility) assigns compliance with target website terms to the customer. Its [General Terms](https://docs.apify.com/legal/general-terms-and-conditions) also make customers responsible for required third-party permissions and require that processed data be authorized to access. Apify operates the Actor platform; it does not grant BA permission. Actor implementation should wait until BA confirms a permitted source/API path.

## If the BA authorizes automated access

Only after written permission or an officially documented retrieval API is confirmed, a narrowly scoped Actor could take:

- `queries`: list of role/search terms (or, if permitted by BA, explicit search URLs)
- `locations`: controlled list (Germany-wide and/or Dresden, as configured)
- `publishedWithinDays`: optional recency bound
- `maxResults`: hard cap, default no higher than the user's per-run limit

and output one normalized job per dataset item:

- `source` (`agentur_fuer_arbeit`)
- `sourceJobId` (BA reference number)
- `title`, `company`, `description`
- `jobUrl`, `applicationUrl` if supplied
- `location`, `employmentType`, `contractDuration`, `workMode` where explicit
- `postedAt` only when present and parseable; preserve unknown rather than inventing a date
- `searchQuery`, `collectedAt`, and `rawSourceFields` for provenance/debugging

This is a proposal for a future authorized implementation, **not** an assertion that the currently observed UI or any backend supports these input or output contracts. Normalize and deduplicate in the parent Python application; do not make the Actor write directly to Airtable.

## Open prerequisite

Before implementation, ask the BA whether a documented API or other authorized automated access method is available for personal job-search collection, and whether it permits storage of job text in a personal Airtable tracker. If authorization is not available, omit BA automated collection and rely on another permitted source or manual links.

## Sources

- BA, [Jobsuche](https://www.arbeitsagentur.de/jobsuche/)
- BA, [official result/search page](https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=jobs)
- BA, [example job detail](https://www.arbeitsagentur.de/jobsuche/jobdetail/19143-0066856655-S)
- BA, [Nutzungsbedingungen](https://www.arbeitsagentur.de/nutzungsbedingungen), especially section 2a(3) and section 4
- BA, [HR-BA-XML interface](https://www.arbeitsagentur.de/unternehmen/arbeitskraefte/hr-ba-xml-schnittstelle)
- Apify, [Actor input schema](https://docs.apify.com/actors/development/actor-definition/input-schema)
- Apify, [Actor input schema specification](https://docs.apify.com/actors/development/actor-definition/input-schema/specification/v1)
- Apify, [Actorization design guidance](https://docs.apify.com/academy/actorization)
- Apify, [run Actor and retrieve dataset data](https://docs.apify.com/academy/api/run-actor-and-retrieve-data-via-api)
- Apify, [shared-responsibility model](https://docs.apify.com/security/shared-responsibility)
- Apify, [General Terms and Conditions](https://docs.apify.com/legal/general-terms-and-conditions)
