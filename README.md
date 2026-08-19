# EdScanner

EdScanner is a local Flask web application for importing public school district
data, searching district websites with simple or structured queries, and
monitoring public district records over time.

The current version imports NCES/ELSI district exports into SQLite, provides
dashboard and district-browsing views, queues website searches in a background
worker, and supports both conservative same-domain crawling and optional Brave
Search API-assisted discovery. It can also discover and reuse district website
built-in search profiles to reduce dependence on third-party search APIs. A
labor-agreement discovery workflow finds and keeps separate contract packages
for each district bargaining unit. A School Boards module discovers public
meeting portals, incrementally collects structured agendas and documents,
preserves revisions, exposes cross-district full-text search, and can schedule
selected working board sources for recurring checks. The two-column home screen
is the application launcher, while the compact header provides Home, Help, and
Settings.

## Local Setup

```powershell
cd C:\projects\edscanner
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the App

```powershell
py app.py
```

Open:

```text
http://127.0.0.1:8765
```

The app runs as a local development server on `127.0.0.1:8765` by default. Set
`EDSCANNER_HOST` and `EDSCANNER_PORT` before starting the app to use a different
bind address or port. Search jobs run
inside the same Flask process, so keep the app process running while queued or
active searches are in progress. The browser page can be closed; the worker does
not depend on the browser staying open.

## Import Districts

To retrieve the current saved NCES/ELSI table:

1. Open the [NCES ELSI Table Generator](https://nces.ed.gov/ccd/elsi/tablegenerator.aspx).
2. Enter Table ID `658416` in the top bar and click **Go**.
3. Select **OK** on the message, then wait for the table to load.
4. Click **CSV** above the table and save the ZIP file in `imports\`.
5. Extract the ZIP so its CSV file is directly inside `imports\`.

The Import page scans `imports\` every time it loads. **Scan imports folder**
performs an explicit refresh, identifies ZIP files that still need extraction,
and lists each supported CSV/XLSX/XLS file with its own import button. The
general form can also auto-select the newest supported file.

You can instead import from the command line:

```powershell
py import_districts.py --auto
```

To provide a specific file:

```powershell
py import_districts.py --source imports\districts.csv --db data\edscanner.db
```

The importer handles NCES/ELSI exports with metadata rows before the header.
Source headers are cleaned so `[District]` and trailing year text are removed
before mapping fields into the database. District websites are normalized, and
only rows with searchable websites are included in search runs. Runtime website
fetches prefer HTTPS even when an imported district URL was originally listed as
HTTP.

## Pages

- Home: a two-column, color-coded launcher for every application module, plus
  database status and recent district searches.
- Help: task-oriented workflow guides, query instructions, status definitions,
  and a district/agency-type glossary.
- Import Districts: retrieve and load district source files from `imports\`.
- Search Profiles: discover and inspect district built-in search profiles.
- Search Websites: configure filters and run simple or advanced website queries.
- Labor Agreements: discover licensed, classified, substitute, administrator,
  transportation, service, and other bargaining-unit agreement packages.
- School Boards: discover public meeting sources, run incremental or historical
  syncs, inspect meetings and evidence, schedule recurring monitoring, review
  run history, and search collected agenda items and documents.
- Districts: browse imported districts, filter by state/name, and sort columns.
- Settings: configure Brave Search and optional ordered Ollama server endpoints.

Operational modules are launched from Home instead of crowding the main header.
School Board pages retain a contextual subnavigation for their related workflow.
The header includes the Save Oregon Schools logo linking to
`https://www.saveoregonschools.com/`, and the footer includes the Save Oregon
Schools copyright, source code, license, and trademark notice links.

## Settings

The Settings page can save a local Brave Search API key to `.env`:

```text
BRAVE_SEARCH_API_KEY="..."
```

The key field is write-only in the web interface; the app shows only whether a
key is saved and a short masked value. The `.env` file is ignored by Git and is
loaded at app startup.

Brave-backed search modes require a saved key. Crawler-only mode does not.

## Search

Use the Search page to enter a simple phrase or advanced expression and optional
filters:

- one or more states
- one or more agency types
- minimum enrollment
- maximum enrollment
- maximum districts for the run
- maximum pages per district
- search method
- Brave API results per district
- follow depth for API-returned pages
- debug logging

Plain multiword input retains the original exact-phrase behavior, so
`community schools` searches for that phrase. Advanced syntax supports:

- uppercase `AND`, `OR`, and `NOT`, with `NOT` evaluated before `AND`, then `OR`
- parentheses for grouping, such as `budget AND (audit OR counsel*) NOT draft`
- quoted phrases, such as `"school board"`
- `*` for zero or more word characters and `?` for exactly one word character,
  such as `counsel*` or `bud?et`

EdScanner parses and validates the expression before creating a run. For a
district-native search, it translates the retrieval query according to the
saved profile's provider capabilities. Providers without Boolean support receive
a bounded set of conservative query variants. Regardless of provider behavior,
EdScanner fetches candidate content and verifies the complete expression locally
before storing a result.

The Search page previews the matching district count. For Brave or Hybrid runs,
it also estimates API calls and approximate listed API cost based on the current
district cap. For district site search runs, it shows how many matching
districts already have working built-in search profiles.

## Search Methods

`Crawler only` uses the built-in same-domain crawler. It does not require an API
key. It starts with the district homepage and sitemap hints, and stores pages
where the query appears in the title, headings, or body. By default EdScanner
does not enforce `robots.txt`; set `EDSCANNER_RESPECT_ROBOTS=true` to opt back
into robots.txt checks.

`Brave API first` sends one Brave Search API request per district using a
domain-limited query such as:

```text
"community schools" site:district.example.org
```

It stores returned API results, fetches those result pages for confirmation and
better snippets, and optionally follows same-domain links one or two levels
deeper.

`Brave with crawler fallback` tries Brave first. If Brave fails or returns no
results for a district, the district falls back to crawler mode.

`District site search` uses a previously discovered built-in district search
profile. It requests the district search results page, extracts same-domain
result links, fetches those pages, and stores only pages where the query is
confirmed in the actual page content. Districts without a working profile are
skipped in this mode.

`District site search with crawler fallback` tries the stored district search
profile first. If no working profile exists or no confirmed results are found,
the district falls back to crawler mode. It does not fall back to Brave.

`District site search with browser for JS profiles` also uses stored district
search profiles. For profiles marked `requires_javascript`, it renders the
search results page in headless Chromium with Playwright, extracts links from
the rendered DOM, then fetches and scores target pages normally.

`District site search with browser and crawler fallback` adds crawler fallback
when no district-search result can be confirmed. It does not fall back to Brave.

Result sources are saved with each hit:

- `brave`: result returned by the Brave API
- `brave+fetch`: Brave result page fetched and confirmed
- `brave-follow`: same-domain page found by following links from a Brave result
- `crawler`: page found by crawler-only or fallback crawling
- `district_search+fetch`: district search result page link fetched and confirmed
- `district_search+fallback_crawler`: crawler result found after district search fallback

## Search Profile Discovery

Use the Search Profiles page to filter districts, queue built-in district search
profile discovery runs, and inspect coverage by status, provider guess,
confidence, test result count, and last discovery date. The profile status
filter supports selecting multiple statuses at once, including `Never tested`.
Discovery runs process in a background worker with a progress page that
auto-refreshes every 15 seconds. Each discovery run can include up to 1,000
districts and uses a bounded worker pool, defaulting to 3 concurrent districts
and configurable with `EDSCANNER_PROFILE_DISCOVERY_WORKERS` or the Search
Profiles form. Discovery is intentionally conservative: it checks same-domain
GET forms, a short list of common search URL patterns, and known platform APIs
where available. Edlio sites are tested through Edlio's JSON search API while
still requiring returned links to belong to the district site. Apptegy sites
that do not expose a simple endpoint are recorded as JavaScript/platform parser
cases instead of saving failed generic search URL guesses. Discovery avoids
login/portal/payment forms and confirms candidate results by fetching returned
content pages. By default it uses a Chrome-on-Windows style user agent and does
not enforce `robots.txt`; set `EDSCANNER_USER_AGENT` to override the request
identity, or `EDSCANNER_RESPECT_ROBOTS=true` to enable robots.txt checks.

You can also run discovery from PowerShell:

```powershell
py discover_search_profiles.py --state OR --limit 100
py discover_search_profiles.py --state OR --agency-type "Regular local school district" --force
py discover_search_profiles.py --district-id 12345 --debug
```

Profiles and test attempts are stored in SQLite in
`district_search_profiles` and `district_search_profile_tests`.

Browser-backed JavaScript profile searches require Playwright's browser runtime:

```powershell
py -m playwright install chromium
```

Challenge/CAPTCHA pages are not bypassed. They are recorded as
`blocked_by_challenge`, and hybrid modes can fall back to the crawler.

Each profile discovery run writes a debug log under:

```text
logs\profile_discovery_runs\
```

The profile discovery run detail page links to that log. Logs include candidate
URL tests, result parsing outcomes, saved statuses, provider guesses, and
per-district completion events.

## Run Status

Searches are queued and processed by a single background worker in the running
Flask app. After submitting a search, the browser redirects to the run detail
page. That page auto-refreshes every 15 seconds while the run is queued or
running.

Run detail pages show:

- status
- districts matched by filters
- districts planned for this run
- searched, in-progress, and remaining district counts
- hits so far
- failures
- elapsed time
- search method and limits
- result groups by district

Queued or running searches can be cancelled from the run detail page.
Cancellation is saved in SQLite. Already stored results are kept, and a running
search stops at the next page or district boundary.

## School Board Monitoring

Open a School Board action from the Home screen. The module provides a complete
workflow for public school-board records:

1. **Discover Sources** follows high-signal governance links on district sites
   and permits known external board-platform hosts without weakening the normal
   district crawler's same-domain boundary. The Discovery scope control makes
   the choice between unchecked districts and rediscovering districts with
   saved sources explicit. Rediscovery starts again from the district website;
   it does not directly health-check the previously saved source URL. A likely
   challenge can receive one bounded Chromium render;
   connection or certificate-chain failures can use a separately bounded browser
   recovery. Rate limits, robots denials, and a remaining CAPTCHA stop without
   browser rotation or challenge bypass. A district is reported as `not_found`
   only after at least one page was inspected; all-page transport failures are
   reported as errors instead. This distinction applies to new runs; older
   `not_found` records may predate it and should be rediscovered if uncertain.
2. **Review Sources** lets an operator validate and save a public portal URL for
   a district when automatic discovery misses or misidentifies it. A source
   becomes working only after the adapter verifies the public endpoint and the
   operator confirms that it belongs to the selected district. Unconfirmed links
   remain manual-review evidence, and prior source URLs remain in history.
3. **Sync** reads public meeting listings, compares stable meeting IDs with
   SQLite, refreshes new/recent/incomplete meetings, and downloads bounded
   public documents.
4. **Meetings** shows normalized metadata, hierarchical agenda items,
   attachments, approved minutes, video links, and revision history.
5. **Search** uses SQLite FTS5 when available (with a LIKE fallback) across
   meetings, agenda items, motions/votes, and extracted document text. Every hit
   retains its district, meeting, entity, retrieval date, and original URL.
6. **Schedules** keeps selected active, working sources current with daily,
   weekly, or monthly monitoring runs.

Source and sync runs are persisted before they enter the in-process board queue.
Their per-district/source work items make restart recovery and historical
backfills idempotent. Cancellation keeps records and versions already saved.
Requests use shared global and per-host concurrency gates, a configurable delay,
finite timeouts, bounded retries, `Retry-After`, within-run URL caching, and
conditional `ETag`/`Last-Modified` document requests where servers support them.

BoardBook provider-directory lookup is available only as an explicit opt-in. Its
public directory exposes names and organization IDs but no state, so EdScanner
uses it only to generate candidates and requires a unique name match plus state
and, when identifiers differ, homepage evidence or a unique reciprocal NCES
name-and-city match before activation. The directory is
fetched once per discovery run and IDs are never enumerated. Because BoardBook's
published terms restrict automated copying, this feature is disabled by default;
enable it only if your organization has confirmed permission under the current
[provider terms](https://www.boardbook.org/boardbook-terms-and-conditions-of-use).
Manual source entry remains available without this option. The initial provider-
directory implementation covers BoardBook only; other platforms continue through
district-site links or manual entry until an authorized directory is documented.

The adapter status for this release is:

- **BoardBook Premier:** end-to-end public listing, structured agenda hierarchy,
  attachments, minutes, video, document download, text extraction, versioning,
  and search.
- **Diligent Community / iCompass and modern CivicClerk:** anonymous public JSON
  listing/detail/document adapters.
- **Legacy BoardDocs and Simbli/eBOARDsolutions:** rendered-public-page parsers
  are included. Sites that return a WAF/Incapsula challenge to ordinary HTTP get
  one bounded Chromium recovery; a remaining challenge is recorded for manual
  review rather than bypassed.
- **Generic:** conservative fallback for obvious public meeting/agenda links.

Monitoring mode prioritizes future meetings, meetings from the configured recent
window, and meetings still lacking approved minutes. Historical backfill accepts
an explicit date range and can be rerun without duplicating meetings, documents,
or versions. Changed normalized agendas and changed document bytes create new
version rows; prior evidence is never silently overwritten.

Board documents are content-addressed on disk rather than stored as SQLite
BLOBs. PDF, HTML, plain text, and DOCX extraction are supported. Image-only PDFs
are retained with a `no_text` status so OCR can be added later; unsupported and
oversized documents are recorded with explicit extraction statuses.

### Scheduled Board Monitoring

Create a schedule for an active, working source from **School Boards →
Schedules** or from that source's row. Each source can have one schedule:

- daily at the selected local time
- weekly on a selected Monday–Sunday
- monthly on day 1–31 at the selected time

Time entry uses an explicit hour, minute, and AM/PM. A monthly day that does not
exist is clamped to that month's final day, so day 31 becomes April 30 and
February 28 or 29 as appropriate. Times use the local wall clock of the computer
running EdScanner.

The scheduler stores its next occurrence and history in SQLite. Atomic expiring
claims and a unique occurrence ledger prevent duplicate dispatch when multiple
workers poll. A due schedule creates an ordinary exact-source monitor run, which
appears in Board Runs and preserves the normal progress, failure, and evidence
behavior. An already queued or running sync for the same source is not overlapped.
After downtime, at most one catch-up occurrence is created before the next future
time is calculated. Schedules can be edited, paused, and re-enabled without
deleting earlier runs or collected records. The scheduler runs with the existing
board worker and is disabled by `EDSCANNER_DISABLE_WORKER=1` during tests.

An optional read-only developer probe verifies a public source without writing
meeting or document rows:

```powershell
py board_probe.py --url https://meetings.boardbook.org/Public/Organization/2221
py board_probe.py --district-id 1234 --since 2026-01-01
py board_probe.py --district-id 1234 --browser
```

The browser option requires the Playwright Chromium runtime described under
Search Profile Discovery. Probes and normal collection use only public listing
pages and links; they do not authenticate or enumerate unpublished object IDs.

## Labor Agreement Discovery

Use the Contracts page to select states and district filters, then scan the
largest matching districts. Discovery follows high-signal Human Resources,
staff, careers, labor-relations, contract, and bargaining links and also checks
district sitemaps. It downloads linked documents, extracts PDF text, identifies
agreement dates, and assembles one package per bargaining unit.

A package never represents the district as a whole. It represents one distinct
unit, such as:

- licensed or certified educators
- classified staff
- substitute educators
- administrators or supervisors
- transportation staff
- service staff
- another or not-yet-identified unit

Each package can contain a base agreement plus extensions, MOUs, amendments,
tentative agreements, and salary schedules. Salary-only pages do not create a
package. When the unit or current status cannot be supported, the package is
marked for review instead of being merged into a known unit. The run detail page
provides the source documents, extracted evidence, editable review fields, and a
CSV export.

Agreement status is evaluated as of the discovery date. An expired agreement is
not assumed to remain operative merely because no successor was found.

### Local Contract Archive and Rescanning

Source documents and extracted-text sidecars are archived by default under:

```text
downloads\contracts\<STATE>\<bounded-district-name>--<NCES-or-database-id>\
```

The state segment is always a two-letter code (or `XX` for malformed source
data). District-name segments are normalized and capped at 50 characters, and a
stable district identifier is appended to prevent collisions. Document names
also include a content-hash suffix. The database records the absolute source and
text paths, byte size, content hash, and archive timestamp, and the run detail
page links to both local copies.

Each completed district scan updates `district_contract_scan_status`, including
the last attempt, last successful scan, result, package count, and document
count. New runs skip districts successfully scanned inside the configured age
threshold (180 days by default). The expired-contract option overrides that age
rule when a bargaining unit has an expired agreement and no newer current
agreement. A force-rescan option bypasses all prior-scan checks.

### Optional Local AI Classification

Contract discovery works without AI. To add a second classification pass,
configure one or more native Ollama servers on the Settings page. Servers are
tried in order, so a Tailscale address can be primary with a LAN address as a
fallback:

```text
EDSCANNER_OLLAMA_ENDPOINTS='["http://server-name:11434","http://192.168.1.10:11434"]'
EDSCANNER_OLLAMA_MODEL="gemma4:12b"
EDSCANNER_LLM_API_KEY="optional"
```

Enable `Use local AI classification` for an individual run. EdScanner sends a
bounded candidate excerpt, link context, title, and URL and requests structured
JSON describing the bargaining unit, union, document role, and effective dates.
EdScanner calls Ollama's native `/api/chat` API. If a server is unavailable or
returns invalid output, the next configured server is tried. If all servers fail,
deterministic results are retained. The bounded excerpt is sent only when local AI
classification is enabled for a run.

## Debug Logs

Enable `Capture debug log` on the Search page to create a per-run text log under:

```text
logs\search_runs\
```

Debug logs include run settings, Brave API requests and returned results, page
fetches, skipped URLs, matches, errors, stored result counts, and cancellation
events. When a debug log exists, the run detail page shows a `Debug log` link.

## Exports

District website search runs can be exported to CSV from their run-detail page.
Those exports include:

- run ID and query
- district details
- result rank
- title
- URL
- content type
- status code
- search source
- score
- snippet

The School Board module also provides CSV exports for:

- the complete filtered Sources view
- the complete filtered Meetings explorer
- filtered Board Search results with meeting, agenda-item/document, retrieval,
  and original-public-source provenance
- discovery-run and sync-run item ledgers with per-district/source status,
  counters, timing, and errors

Export links preserve active filters but intentionally omit pagination so the
CSV contains the full filtered result set. Columns and row ordering are stable,
dates and required queries are validated, missing runs return 404, and text
cells are guarded against spreadsheet-formula interpretation.

Generated exports are written under `exports\` and are ignored by Git.

## Local Files

Runtime files are intentionally local and ignored by Git:

```text
.env
data\edscanner.db
imports\*
exports\*
downloads\contracts\*
data\board_documents\*
data\board_snapshots\*
logs\*.log
logs\search_runs\*
logs\board_discovery_runs\*
logs\board_sync_runs\*
.venv\
__pycache__\
```

The repository keeps `.gitkeep` placeholders for `data\`, `imports\`,
`exports\`, and `logs\` so the directory structure exists after clone.

## Configuration

Optional environment variables can be set in PowerShell or saved in `.env`:

```powershell
$env:EDSCANNER_DB_PATH="C:\projects\edscanner\data\edscanner.db"
$env:EDSCANNER_USER_AGENT="EdScanner/0.1 (+https://github.com/SaveOregonSchools/edscanner; public school district content search)"
$env:EDSCANNER_MAX_PAGES_PER_DISTRICT="100"
$env:EDSCANNER_MAX_RESULTS_PER_DISTRICT="5"
$env:EDSCANNER_REQUEST_TIMEOUT="15"
$env:EDSCANNER_REQUEST_DELAY="0.75"
$env:EDSCANNER_MAX_PDF_SIZE_MB="10"
$env:EDSCANNER_MAX_HTML_SIZE_MB="5"
$env:EDSCANNER_MAX_TOTAL_DISTRICTS_PER_RUN="25"
$env:EDSCANNER_PROFILE_DISCOVERY_WORKERS="3"
$env:EDSCANNER_CONTRACT_DISCOVERY_WORKERS="3"
$env:EDSCANNER_CONTRACT_RESCAN_DAYS="180"
$env:EDSCANNER_CONTRACT_DISTRICT_DIR_NAME_MAX="50"
$env:EDSCANNER_BOARD_WORKERS="4"
$env:EDSCANNER_BOARD_PER_HOST_WORKERS="2"
$env:EDSCANNER_BOARD_REQUEST_DELAY="0.75"
$env:EDSCANNER_BOARD_HTTP_MAX_REDIRECTS="5"
$env:EDSCANNER_BOARD_HTTP_CACHE_MAX_ENTRIES="128"
$env:EDSCANNER_BOARD_HTTP_CACHE_MAX_MB="32"
$env:EDSCANNER_BOARD_PROVIDER_DIRECTORY_ENABLED="false"
$env:EDSCANNER_BOARD_ALLOW_PRIVATE_NETWORKS="false"
$env:EDSCANNER_BOARD_IPV4_ONLY="true"
$env:EDSCANNER_BOARD_ALLOW_INSECURE_SSL_FALLBACK="false"
$env:EDSCANNER_BOARD_INSECURE_SSL_HOSTS="legacy-board.example.org"
$env:EDSCANNER_BOARD_MAX_DOCUMENT_MB="25"
$env:EDSCANNER_BOARD_MAX_DOCUMENTS_PER_MEETING="100"
$env:EDSCANNER_BOARD_MAX_MEETINGS_PER_SOURCE="250"
$env:EDSCANNER_BOARD_RECENT_RECHECK_DAYS="60"
$env:EDSCANNER_BOARD_INCOMPLETE_RECHECK_DAYS="14"
$env:EDSCANNER_BOARD_OLD_RECHECK_DAYS="90"
$env:EDSCANNER_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
$env:EDSCANNER_VERIFY_SSL="true"
$env:EDSCANNER_RESPECT_ROBOTS="false"
$env:EDSCANNER_FLASK_DEBUG="false"
$env:EDSCANNER_HOST="127.0.0.1"
$env:EDSCANNER_PORT="8765"
$env:EDSCANNER_SECRET_KEY="optional-long-random-value-for-stable-sessions"
$env:BRAVE_SEARCH_API_KEY="..."
$env:EDSCANNER_OLLAMA_ENDPOINTS='["http://server-name:11434","http://192.168.1.10:11434"]'
$env:EDSCANNER_OLLAMA_MODEL="gemma4:12b"
$env:EDSCANNER_LLM_API_KEY="optional"
```

Board collection rejects localhost, private, link-local, and reserved network
targets by default, validates each bounded redirect hop, and never silently
retries with TLS verification disabled. Ordinary hosts use Requests' normal
certificate verifier. The exact compatibility host `meetings.boardbook.org`
uses the operating system's native trust store for Windows certificate-chain
handling; this is not extended to arbitrary district or manually entered hosts.
Board networking is IPv4-only by default (`EDSCANNER_BOARD_IPV4_ONLY=true`) so
an unusable IPv6 route cannot mask the IPv4 result; IPv6 can be explicitly
re-enabled for an environment that has verified connectivity. Each ordinary
connection is pinned to its validated DNS answer while retaining the public
hostname for HTTP Host, TLS SNI, and certificate verification.
Playwright fallback uses a loopback SOCKS proxy that
applies the same address validation/pinning to every browser tunnel; unnecessary
WebSockets and non-HTTP network schemes are blocked. `EDSCANNER_BOARD_ALLOW_PRIVATE_NETWORKS=true`
is intended only for deterministic local-server tests. Insecure TLS fallback
also requires both an explicit opt-in and a comma-separated exact/domain-suffix
host allowlist in `EDSCANNER_BOARD_INSECURE_SSL_HOSTS`; affected responses are
logged and marked with `insecure_tls` provenance. The per-client board response
cache is an LRU bounded by both entry count and total bytes.

Use `EDSCANNER_DISABLE_WORKER=1` only for tests or diagnostics when the
background worker should not start automatically.

Application logs are written to:

```text
logs\edscanner.log
```

## Testing

Run compile checks:

```powershell
.\.venv\Scripts\python -m compileall -q app.py common.py import_districts.py search_engine.py site_search_discovery.py discover_search_profiles.py contract_discovery.py ai_matcher.py board board_probe.py
```

Run unit tests:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

The test suite includes local HTTP-server coverage for crawler mode, Brave API
mode using a fake local endpoint, district search profile discovery and reuse,
CSV export, debug-log creation, and cancellation before run start. School-board
tests use checked-in HTML/JSON fixtures and local/fake HTTP only; they cover all
platform detectors/parsers plus source discovery boundaries, BoardBook
normalization, persistence, revision history, document extraction, FTS, sync
idempotency, cancellation, scheduler calculation/claims/concurrency, and Flask
routes. Advanced-query tests cover parsing, provider translation, local matching,
and safe syntax errors. Home, Help, and Import route tests cover the module
launcher and NCES workflow. Live websites are never required by the ordinary
test suite.

## License

EdScanner's software code is copyright (C) 2026 Save Oregon Schools, LLC and is
licensed under the GNU Affero General Public License version 3. See
`LICENSE` for the full license text.

EdScanner is distributed without any warranty; without even the implied warranty
of merchantability or fitness for a particular purpose.

The Save Oregon Schools name, logo, and related branding are not licensed for
reuse under the GNU Affero General Public License. See `TRADEMARKS.md` for the
project's trademark and branding notice.

## Current Limitations

- Searches depend on the local Flask worker process staying open.
- Search profile discovery batches launched from the web UI are capped and run
  through the local Flask worker process.
- Search operators are intentionally uppercase; lowercase words such as `and`
  remain ordinary search text for backward compatibility.
- Search-profile providers vary in native query support, so retrieval can use
  multiple conservative variants before EdScanner verifies the expression
  locally.
- Brave mode consumes one API request per district searched.
- PDF parsing is basic and limited by file size.
- Scanned image-only agreements are flagged by their missing extracted text;
  OCR is not yet built in.
- Scanned image-only board documents are retained but are not OCR'd.
- Legacy BoardDocs and some Simbli sites may require Playwright or manual review
  because their public pages are JavaScript-driven or protected by bot
  challenges. Challenges and authenticated portals are never bypassed.
- `robots.txt` is enforced when `EDSCANNER_RESPECT_ROBOTS=true`; the default is
  disabled consistently across existing and board collectors.
- The crawler is intentionally conservative and uses per-run district and page
  caps.

## Planned Features

- persistent district website indexing
- scheduled re-crawls
- AI-assisted match classification
- semantic search
- richer PDF-first search workflows
- saved board-record watchlists and change notifications
- saved search projects and watchlists
