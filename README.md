# EdScanner

EdScanner is a local Flask web application for importing public school district data and searching district websites for keywords or exact text strings.

The first version imports an NCES/ELSI district spreadsheet, normalizes district fields into SQLite, filters districts by state, agency type, and enrollment, and returns up to five matching pages per district website searched.

## Local Setup

```powershell
cd C:\projects\edscanner
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Import Districts

Place a `.csv` or `.xlsx` district source file in `imports\`, then run:

```powershell
py import_districts.py --auto
```

Or provide a specific file:

```powershell
py import_districts.py --source imports\districts.csv --db data\edscanner.db
```

The importer handles ELSI exports with metadata rows before the header. Source headers are cleaned so `[District]` and trailing year text are removed before mapping fields into the database.

## Run the App

```powershell
py app.py
```

Open:

```text
http://127.0.0.1:5000
```

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

Only districts with a normalized, searchable website are searched. Each search run stores matching pages in SQLite and can be exported to CSV from the run detail page.

Current search syntax is simple case-insensitive text matching. Enter one word
or an exact phrase. Boolean operators, wildcards, and quote parsing are planned
future improvements.

Searches are queued and processed by a single background worker in the running
Flask app. After submitting a search, the browser redirects to the run detail
page, which auto-refreshes every 15 seconds while the run is queued or running.
The browser does not need to stay open for the worker to continue, but the local
Flask process must keep running.

Search methods:

- `Crawler only` uses the built-in same-domain crawler and does not require an API key.
- `Brave API first` sends one Brave Search API request per district, stores returned results, fetches those pages, and optionally follows links one or two levels deeper.
- `Brave with crawler fallback` uses Brave first and falls back to crawler mode if the API call fails or returns no results.

The Settings page can save a local Brave Search API key to `.env`:

```text
BRAVE_SEARCH_API_KEY="..."
```

The `.env` file is ignored by Git.

## Configuration

Optional environment variables:

```powershell
$env:EDSCANNER_DB_PATH="C:\projects\edscanner\data\edscanner.db"
$env:EDSCANNER_MAX_PAGES_PER_DISTRICT="100"
$env:EDSCANNER_MAX_RESULTS_PER_DISTRICT="5"
$env:EDSCANNER_REQUEST_TIMEOUT="15"
$env:EDSCANNER_REQUEST_DELAY="0.75"
$env:EDSCANNER_MAX_PDF_SIZE_MB="10"
$env:EDSCANNER_MAX_TOTAL_DISTRICTS_PER_RUN="25"
$env:EDSCANNER_VERIFY_SSL="true"
$env:BRAVE_SEARCH_API_KEY="..."
```

Logs are written to:

```text
logs\edscanner.log
```

## Current Limitations

- Searches depend on the local Flask worker process staying open.
- Matching is case-insensitive exact phrase matching.
- PDF parsing is basic and limited by file size.
- Robots.txt is respected where it can be fetched and parsed.
- The crawler is intentionally conservative and uses a per-run district safety cap.

## Planned Features

- persistent district website indexing
- scheduled re-crawls
- AI-assisted match classification
- semantic search
- PDF-first search workflows
- school board agenda and policy document detection
- saved search projects and watchlists
