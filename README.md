# EdScanner

EdScanner is a local Flask web application for importing public school district
data and searching district websites for keywords or exact text strings.

The current version imports NCES/ELSI district exports into SQLite, provides
dashboard and district-browsing views, queues website searches in a background
worker, and supports both conservative same-domain crawling and optional Brave
Search API-assisted discovery. It can also discover and reuse district website
built-in search profiles to reduce dependence on third-party search APIs.

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
http://127.0.0.1:5000
```

The app runs as a local development server on `127.0.0.1:5000`. Search jobs run
inside the same Flask process, so keep the app process running while queued or
active searches are in progress. The browser page can be closed; the worker does
not depend on the browser staying open.

## Import Districts

Place a `.csv` or `.xlsx` district source file in `imports\`, then import it
from the Import page or run:

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

- Home: database summary, state coverage, import status, and recent runs.
- Import: load district source files from `imports\`.
- Search: configure filters and run website searches.
- Search Profiles: discover and inspect district built-in search profiles.
- Districts: browse imported districts, filter by state/name, and sort columns.
- Settings: save or clear the local Brave Search API key.

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

Use the Search page to enter a keyword or phrase and optional filters:

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

Current search syntax is simple case-insensitive text matching. Enter one word
or an exact phrase, such as `calendar` or `community schools`. Boolean logic,
wildcards, and quote parsing are not currently supported.

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

## Debug Logs

Enable `Capture debug log` on the Search page to create a per-run text log under:

```text
logs\search_runs\
```

Debug logs include run settings, Brave API requests and returned results, page
fetches, skipped URLs, matches, errors, stored result counts, and cancellation
events. When a debug log exists, the run detail page shows a `Debug log` link.

## Exports

Each run can be exported to CSV from the run detail page. Exports include:

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

Generated exports are written under `exports\` and are ignored by Git.

## Local Files

Runtime files are intentionally local and ignored by Git:

```text
.env
data\edscanner.db
imports\*
exports\*
logs\*.log
logs\search_runs\*
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
$env:EDSCANNER_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
$env:EDSCANNER_VERIFY_SSL="true"
$env:EDSCANNER_RESPECT_ROBOTS="false"
$env:EDSCANNER_FLASK_DEBUG="false"
$env:BRAVE_SEARCH_API_KEY="..."
```

Use `EDSCANNER_DISABLE_WORKER=1` only for tests or diagnostics when the
background worker should not start automatically.

Application logs are written to:

```text
logs\edscanner.log
```

## Testing

Run compile checks:

```powershell
.\.venv\Scripts\python -m py_compile app.py common.py import_districts.py search_engine.py site_search_discovery.py discover_search_profiles.py ai_matcher.py
```

Run unit tests:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

The test suite includes local HTTP-server coverage for crawler mode, Brave API
mode using a fake local endpoint, district search profile discovery and reuse,
CSV export, debug-log creation, and cancellation before run start.

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
- Search matching is simple case-insensitive word or phrase matching.
- Boolean operators, wildcards, and quote parsing are not implemented.
- Brave mode consumes one API request per district searched.
- PDF parsing is basic and limited by file size.
- Robots.txt is respected where it can be fetched and parsed.
- The crawler is intentionally conservative and uses per-run district and page
  caps.

## Planned Features

- persistent district website indexing
- scheduled re-crawls
- AI-assisted match classification
- semantic search
- richer PDF-first search workflows
- school board agenda and policy document detection
- saved search projects and watchlists
