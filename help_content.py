from __future__ import annotations

from collections import OrderedDict
from typing import Any


NCES_TABLE_URL = "https://nces.ed.gov/ccd/elsi/tablegenerator.aspx"


HELP_TOPICS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        (
            "getting-started",
            {
                "title": "Getting Started",
                "summary": (
                    "A practical order of operations for loading districts, preparing their "
                    "websites, and using EdScanner's research and monitoring modules."
                ),
                "steps": [
                    (
                        "Import current districts",
                        "Download the saved NCES ELSI table, place its CSV in imports, and run the district import.",
                    ),
                    (
                        "Review your target districts",
                        "Use Districts to confirm names, agency types, enrollment, and website coverage.",
                    ),
                    (
                        "Discover search profiles",
                        "For district-native site search, test the target districts first so EdScanner knows each site's search provider and URL pattern.",
                    ),
                    (
                        "Search district content",
                        "Choose a search method, enter a simple or advanced query, preview the district count, and start a persistent run.",
                    ),
                    (
                        "Set up specialized monitoring",
                        "Discover and sync school-board sources, or scan district sites for labor agreements.",
                    ),
                ],
                "sections": [
                    {
                        "title": "Which steps are required?",
                        "paragraphs": [
                            "District import is the foundation for every district-based module.",
                            "Search Profile discovery is recommended before using a District site search method. It is not required for Brave API or crawler-only searches.",
                            "School Board discovery must find a source before that district can be synced, searched, or scheduled.",
                        ],
                    },
                    {
                        "title": "Persistent runs",
                        "bullets": [
                            "Long-running searches, profile discovery, contract scans, and board syncs continue in background workers after you leave the page.",
                            "Run-detail pages show progress, failures, logs, and cancellation controls.",
                            "Cancellation preserves results already saved.",
                        ],
                    },
                ],
                "related": [
                    ("import_page", "Import Districts"),
                    ("districts_page", "Districts"),
                    ("search_profiles_page", "Search Profiles"),
                    ("search_page", "Search"),
                ],
            },
        ),
        (
            "import-districts",
            {
                "title": "Importing Districts",
                "summary": (
                    "Load the current NCES Common Core of Data district directory that powers "
                    "EdScanner's district filters, websites, and enrollment ranges."
                ),
                "steps": [
                    ("Open NCES ELSI", "Open the Table Generator and enter Table ID 658416 in the top bar."),
                    ("Load the saved table", "Click Go, acknowledge the message with OK, and wait for the district table to appear."),
                    ("Export CSV", "Click CSV above the results and save the downloaded ZIP file in EdScanner's imports folder."),
                    ("Extract the ZIP", "Unpack it in imports so the resulting CSV file is directly inside that folder."),
                    ("Scan and import", "Return to Import Districts, scan the folder, select the CSV, and run the import."),
                ],
                "sections": [
                    {
                        "title": "How file discovery works",
                        "paragraphs": [
                            "The Import page scans the imports folder every time it loads. The Scan imports folder button is an explicit refresh after you copy or extract a file.",
                            "CSV, XLSX, and XLS files are listed newest first. Auto-select newest import file uses the first file in that list.",
                            "The importer detects the NCES header row, maps the required fields, and updates existing districts instead of blindly duplicating them.",
                        ],
                    },
                    {
                        "title": "Required information",
                        "terms": [
                            ("Agency name", "The public name of the district or education agency."),
                            ("NCES agency ID", "The stable federal identifier used when available to match future imports."),
                            ("State", "The two-letter state or jurisdiction code used by filters."),
                            ("Agency type", "NCES's classification of the organization."),
                            ("Website", "The public website EdScanner will search or inspect."),
                            ("Enrollment", "Total student enrollment excluding adult education, used for size filters."),
                        ],
                    },
                ],
                "external_links": [(NCES_TABLE_URL, "Open the NCES ELSI Table Generator")],
                "related": [("import_page", "Open Import Districts"), ("districts_page", "Review Districts")],
            },
        ),
        (
            "search-profiles",
            {
                "title": "Search Profiles",
                "summary": (
                    "Teach EdScanner how each district website's own search feature works before "
                    "running profile-based content searches."
                ),
                "steps": [
                    ("Choose target districts", "Filter by state, agency type, enrollment, current profile status, or provider."),
                    ("Use a neutral test query", "Choose a common term such as calendar that should produce legitimate district results."),
                    ("Discover profiles", "EdScanner checks likely search links, forms, platform patterns, and result pages."),
                    ("Review outcomes", "Focus on working profiles first, then investigate manual-review, JavaScript, blocked, or failed rows."),
                    ("Run Search", "Select a District site search method to use the working profiles."),
                ],
                "sections": [
                    {
                        "title": "Profile status",
                        "terms": [
                            ("Never tested", "No profile-discovery result has been saved for this district."),
                            ("working", "A public search endpoint was tested and returned plausible district results."),
                            ("no_search_found", "Discovery did not find a credible district search interface."),
                            ("manual_review", "A likely search feature exists, but a person should confirm how it works."),
                            ("search_found_but_failed", "A search interface was found, but the test request or result validation failed."),
                            ("requires_javascript", "The search UI or its results require a rendered browser."),
                            ("blocked_by_challenge", "A bot/WAF challenge prevented a reliable anonymous test."),
                            ("blocked_by_robots", "The site's robots policy disallowed the attempted fetch."),
                            ("external_search_only", "Reserved for a site that can only be searched through a separate public service; current discovery may instead flag that case for review."),
                            ("error", "Discovery ended with an unexpected request, parsing, or persistence error."),
                        ],
                    },
                    {
                        "title": "Other fields",
                        "terms": [
                            ("Provider guess", "The platform EdScanner believes powers the search, such as Edlio, Finalsite, or a custom form."),
                            ("Confidence", "A relative 0-100 signal based on platform markers, successful requests, and validated results."),
                            ("Results", "How many plausible results the discovery test observed."),
                            ("Search URL template", "The saved endpoint with a query placeholder used during future district-site searches."),
                            ("Rediscover existing profiles", "Retest districts even when they already have a saved result; useful after a site redesign."),
                        ],
                    },
                ],
                "related": [("search_profiles_page", "Open Search Profiles"), ("search_page", "Open Search")],
            },
        ),
        (
            "search",
            {
                "title": "Searching District Websites",
                "summary": (
                    "Search selected district websites with native site search, Brave results, a "
                    "bounded crawler, or a combined strategy."
                ),
                "steps": [
                    ("Prepare profiles when appropriate", "Run Search Profiles first if you plan to use a District site search method."),
                    ("Write the query", "Use terms, quoted phrases, wildcards, AND, OR, NOT, and parentheses to express the content you need."),
                    ("Select scope", "Choose states, agency types, enrollment range, and a safe maximum number of districts."),
                    ("Choose a method", "Pick the retrieval strategy that best fits profile coverage, API availability, and desired completeness."),
                    ("Preview, run, and review", "Check the matching count, start the run, then review grouped results, errors, evidence, and export."),
                ],
                "sections": [
                    {
                        "title": "Query syntax",
                        "bullets": [
                            "A plain multiword query keeps EdScanner's original exact-phrase behavior: community schools searches for that phrase.",
                            "Use explicit uppercase AND when both expressions must match: mental AND health.",
                            "Use OR for alternatives: levy OR bond.",
                            "Use NOT to exclude a term: curriculum NOT athletics.",
                            "Use quotes for an exact phrase: \"community schools\".",
                            "Use * for zero or more characters in a word: counsel* matches counsel, counselor, and counseling.",
                            "Use ? to replace one character inside a word: bud?et matches budget.",
                            "Use parentheses for grouping: (levy OR bond) AND election.",
                            "Operators must be uppercase. EdScanner adapts retrieval to each provider but verifies the full expression locally against fetched content.",
                        ],
                    },
                    {
                        "title": "Search methods",
                        "terms": [
                            ("Brave API first", "Ask Brave for site-restricted results and then fetch the returned public pages."),
                            ("Brave with crawler fallback", "Use Brave first and crawl the district when API results are unavailable or insufficient."),
                            ("District site search", "Use each saved working profile and skip districts without a usable profile."),
                            ("District site search with crawler fallback", "Use the profile when possible and crawl when it is unavailable or fails."),
                            ("District site search with browser", "Render JavaScript-backed profile result pages in a bounded browser."),
                            ("Crawler only", "Follow same-organization links from the district site without a search API."),
                        ],
                    },
                    {
                        "title": "Run fields",
                        "terms": [
                            ("Max districts", "A safety cap on how many matching districts this run may process."),
                            ("Max pages per district", "A per-district bound on fetched pages during crawling and follow-up."),
                            ("API results per district", "How many Brave results to request for each district."),
                            ("Follow depth", "How many same-site link levels to inspect beyond a result page."),
                            ("Search workers", "The number of districts processed concurrently; higher is faster but creates more traffic."),
                            ("Capture debug log", "Save detailed request and decision events for troubleshooting the run."),
                        ],
                    },
                ],
                "related": [("search_page", "Open Search"), ("search_profiles_page", "Open Search Profiles")],
            },
        ),
        (
            "contracts",
            {
                "title": "Labor Agreement Discovery",
                "summary": "Find, archive, classify, and review public collective-bargaining agreements and salary schedules.",
                "steps": [
                    ("Select districts", "Filter the imported district population and set a bounded run size."),
                    ("Choose scan options", "Decide whether to include salary schedules, archive files, extract text, or use optional local AI classification."),
                    ("Run discovery", "EdScanner inspects high-signal staff, HR, labor, contract, and sitemap pages."),
                    ("Review packages", "Verify the bargaining unit, union, effective dates, expiration, and document evidence."),
                    ("Recheck over time", "Use expiration-aware and age-based rescanning instead of repeatedly scanning fresh records."),
                ],
                "sections": [
                    {
                        "title": "Important terms",
                        "terms": [
                            ("Package", "One distinct bargaining unit and its related agreement documents; not the district as a whole."),
                            ("Current / expired / future", "Agreement status derived from effective and expiration dates when available."),
                            ("Needs review", "Evidence was found, but classification or dates need human confirmation."),
                            ("Archive documents", "Store a bounded local copy with its source URL and retrieval metadata."),
                            ("Extracted text", "Searchable text produced from supported archived document formats."),
                        ],
                    },
                    {
                        "title": "Bargaining-unit types",
                        "terms": [
                            ("licensed / certified", "Teachers and other employees whose jobs require an educator license or certificate."),
                            ("classified", "Non-licensed support employees such as instructional assistants, custodians, or office staff."),
                            ("substitute", "Substitute teachers or other substitute employees covered by a distinct agreement."),
                            ("administrator / supervisor", "Principals, administrators, or supervisory employees in a separate bargaining unit."),
                            ("transportation", "Bus drivers, mechanics, or other transportation employees in a distinct unit."),
                            ("service", "Food service, maintenance, facilities, or other service employees in a distinct unit."),
                            ("other / unknown", "A distinct package whose unit does not match a known category or still needs review."),
                        ],
                    },
                ],
                "related": [("contracts_page", "Open Contracts")],
            },
        ),
        (
            "school-boards",
            {
                "title": "School Board Monitoring",
                "summary": (
                    "Discover public board portals, collect structured meetings and documents, "
                    "search the archive, and keep selected districts current on a schedule."
                ),
                "steps": [
                    ("Discover Sources", "Identify the public board portal and platform for selected districts."),
                    ("Review Sources", "Confirm working sources and use Add or correct source when discovery misses or misidentifies a district."),
                    ("Sync Meetings", "Collect meeting metadata, agenda hierarchy, minutes, packets, attachments, and revisions."),
                    ("Explore or Search", "Browse meetings by district/date or search indexed agenda and document text."),
                    ("Schedule monitoring", "Enable a daily, weekly, or monthly sync for boards you want to keep current."),
                ],
                "sections": [
                    {
                        "title": "Do I have to run discovery first?",
                        "paragraphs": [
                            "Usually. Discovery is the fastest way to identify sources in bulk, but you can instead open Board Sources and use Add or correct source for an individual district.",
                            "A manually submitted URL is safely fetched and checked by a platform adapter. It becomes working only after the adapter verifies the endpoint and you confirm that the page belongs to the selected district; otherwise it remains manual-review evidence.",
                            "Discovery does not collect the full meeting archive. Sync performs that collection and creates versions only when normalized content or document bytes actually change.",
                        ],
                    },
                    {
                        "title": "Challenges and provider directories",
                        "paragraphs": [
                            "When a district page returns a likely access challenge, EdScanner can try that page once with a real Chromium browser. Rate limits and robots denials do not trigger the retry, and a remaining CAPTCHA or challenge is recorded for manual review rather than bypassed.",
                            "BoardBook publishes an organization directory, but the directory has names and opaque organization IDs without state. Provider-directory lookup is therefore disabled by default and must only be enabled after the operator confirms permission under the provider's current terms.",
                            "When authorized, EdScanner fetches the directory once per discovery run, generates local name candidates, and requires state evidence plus a unique reciprocal NCES name-and-city match (or district-homepage evidence) before a source can become working. Ambiguous or state-less matches are never activated automatically.",
                        ],
                    },
                    {
                        "title": "Platform and source terms",
                        "terms": [
                            ("BoardBook", "Sparq's public BoardBook Premier meeting and agenda portal."),
                            ("Diligent Community", "Current or legacy iCompass/Diligent public meeting portals and APIs."),
                            ("CivicClerk", "Modern CivicClerk public event and meeting portal."),
                            ("BoardDocs", "Legacy Diligent BoardDocs portal; some sites require browser rendering or manual review."),
                            ("Simbli", "eBOARDsolutions/Simbli portal; some deployments return a browser challenge to ordinary HTTP."),
                            ("Working source", "A current public portal EdScanner can identify and process."),
                            ("Platform changed/parser broken", "A previously working source now returns content the adapter cannot reliably interpret."),
                            ("Version", "An immutable historical snapshot created only when meaningful meeting or document content changes."),
                        ],
                    },
                    {
                        "title": "Board source status",
                        "terms": [
                            ("working", "The active public source was identified and its adapter can process it."),
                            ("manual_review", "A plausible or manually entered source is retained, but adapter validation has not confirmed it as working."),
                            ("requires_javascript", "The public portal requires bounded browser rendering for reliable collection."),
                            ("blocked_by_challenge", "A public WAF or bot challenge prevented reliable anonymous collection."),
                            ("blocked_by_robots", "The configured robots policy disallowed the attempted public request."),
                            ("platform_changed_or_parser_broken", "A previously usable platform now returns an unexpected structure and needs adapter review."),
                            ("not_found", "Discovery did not find a credible public board portal for the district."),
                            ("error", "Discovery or sync ended with an unexpected request, parsing, or persistence error."),
                        ],
                    },
                    {
                        "title": "Sync and document fields",
                        "terms": [
                            ("Monitor", "Refresh future, recent, or incomplete meetings and avoid repeatedly refetching stable old records."),
                            ("Historical backfill", "Collect meetings inside an explicit past date range without duplicating existing identities or versions."),
                            ("Force", "Bypass normal freshness checks and request the selected source records again."),
                            ("Extraction status", "Whether document text was extracted, empty, unsupported, oversized, or failed while the original evidence was retained."),
                            ("Last checked", "The latest collection attempt, whether it succeeded or failed."),
                            ("Last successful sync", "The latest clean sync; cancelled or failed work does not advance it."),
                            ("Current source", "The active portal for the district; older or weaker alternate sources remain as historical evidence."),
                        ],
                    },
                    {
                        "title": "Schedule behavior",
                        "bullets": [
                            "Daily schedules run every day at the chosen local time.",
                            "Weekly schedules run on the selected weekday at that time.",
                            "Monthly schedules run on the selected day; if that day does not exist, EdScanner uses the month's final day.",
                            "Schedules create ordinary persistent sync runs, so progress and failures remain visible in Runs.",
                        ],
                    },
                ],
                "related": [
                    ("boards.discovery", "Discover Board Sources"),
                    ("boards.sources", "Review Board Sources"),
                    ("boards.sync", "Sync Board Meetings"),
                    ("boards.meetings", "Explore Meetings"),
                    ("boards.search", "Search Board Content"),
                    ("boards.schedules", "Monitoring Schedules"),
                    ("boards.runs", "Board Run History"),
                ],
            },
        ),
        (
            "glossary",
            {
                "title": "District and Filter Glossary",
                "summary": "Short definitions for the district classifications and common filters used throughout EdScanner.",
                "sections": [
                    {
                        "title": "Common agency types",
                        "terms": [
                            ("1 — Regular local district, not part of a supervisory union", "An autonomous local public-school district not administered through a supervisory union."),
                            ("2 — Local district that is part of a supervisory union", "A local district that shares or receives administration through a supervisory union."),
                            ("3 — Supervisory union administrative center/county superintendent", "An office providing administration or supervision across multiple local districts."),
                            ("4 — Regional Education Service Agency", "A regional public agency providing shared educational or operational services to districts."),
                            ("5 — State instructional agency", "A state-operated agency that directly provides elementary or secondary instruction."),
                            ("6 — Federal instructional agency", "A federally operated agency that directly provides elementary or secondary instruction."),
                            ("7 — Independent Charter District", "A charter local education agency operating independently of a traditional district."),
                            ("8 — Other education agency", "A public education agency that does not fit the more specific NCES categories."),
                            ("9 — Specialized public school district", "A public district focused on a specialized population, service, or instructional purpose."),
                        ],
                    },
                    {
                        "title": "District fields",
                        "terms": [
                            ("State", "Two-letter state or jurisdiction abbreviation from the imported NCES data."),
                            ("NCES agency ID", "Federal identifier for a local education agency."),
                            ("Enrollment excludes AE", "Reported student total excluding adult education; blank means the source did not provide a usable value."),
                            ("Searchable website", "A district row with a usable normalized public HTTP(S) website."),
                            ("Matching districts", "All imported rows satisfying the filters before the run's maximum-district cap."),
                            ("Planned districts", "The bounded subset recorded for a particular persistent run."),
                            ("Provider", "The website or meeting-platform vendor inferred from public technical markers."),
                            ("Confidence", "A relative evidence score, not a statistical probability."),
                        ],
                    },
                    {
                        "title": "Run status",
                        "terms": [
                            ("queued", "Saved and waiting for a background worker."),
                            ("running", "Actively processing one or more planned items."),
                            ("completed", "The worker finished; individual items may still show isolated failures."),
                            ("failed", "The run could not complete because of a run-level error."),
                            ("cancelled", "Stopped by request; already-persisted results are retained."),
                        ],
                    },
                ],
                "related": [("districts_page", "Open Districts")],
            },
        ),
        (
            "settings",
            {
                "title": "Settings",
                "summary": "Configure optional external search and local AI services used by selected EdScanner modules.",
                "sections": [
                    {
                        "title": "Brave Search",
                        "paragraphs": [
                            "A Brave Search API key enables the Brave-first and Brave-with-crawler-fallback district search methods. Keys are stored in the local .env file, not in search-run rows.",
                        ],
                    },
                    {
                        "title": "Local AI / Ollama",
                        "paragraphs": [
                            "One or more Ollama endpoints and a model can optionally assist contract classification. Board collection and ordinary district search do not require AI.",
                            "Use Test connection before starting a large AI-assisted scan. Endpoints are tried in priority order.",
                        ],
                    },
                ],
                "related": [("settings_page", "Open Settings")],
            },
        ),
    ]
)


def help_topic(slug: str) -> dict[str, Any] | None:
    topic = HELP_TOPICS.get(str(slug or "").strip().casefold())
    return dict(topic) if topic else None


def help_topic_list() -> list[dict[str, str]]:
    return [
        {"slug": slug, "title": str(topic["title"]), "summary": str(topic["summary"])}
        for slug, topic in HELP_TOPICS.items()
    ]


__all__ = ["HELP_TOPICS", "NCES_TABLE_URL", "help_topic", "help_topic_list"]
