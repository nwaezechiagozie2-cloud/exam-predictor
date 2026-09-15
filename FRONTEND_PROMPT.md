# Build the ExamOracle frontend

You are building the frontend for **ExamOracle**, a study-priority predictor: it takes
past exam papers, extracts every question, analyzes which topics the examiner keeps
coming back to year after year, and predicts the most likely topics (with evidence) for
the next exam. This is for a 2-hour school hackathon — prioritize a working, impressive
demo over completeness.

## Stack constraints (hard requirements)

- Vanilla HTML/CSS/JS in a **single `index.html`** (CSS and JS inline). No frameworks,
  no build step, no npm, no CDN dependencies. It must work when served as a static file.
- The file lives at `/home/chiagozie/exam-predictor/app/index.html`. The backend serves
  that directory at its root, so after you write the file, the app is at
  `http://localhost:8787/`.
- All backend calls are same-origin `fetch` to relative paths (e.g. `fetch('/analyze')`).
- Persist user-added data + settings in `localStorage` under keys `examoracle-rows` and
  `examoracle-settings`.

## Running the backend

```
cd /home/chiagozie/exam-predictor && python3 server.py
```
It serves on `http://localhost:8787`. A static `data/` directory next to it holds the
raw extracted JSON (you don't need it — use the API below).

## The data model

Everything is a **row** — one exam question:

```json
{
  "course": "18.01",          // course id, short string, e.g. "18.01", "my-school-math"
  "year": 2006,               // integer
  "paper": "exam1",           // paper id within the course+year, short string
  "q": "3",                   // question number, or subpart like "3a" (string)
  "topic": "implicit-differentiation",  // canonical snake_case topic label
  "marks": 15,                // point value, number or null
  "text": "Find dy/dx for y defined implicitly by y^4 + xy = 4"  // short summary
}
```

## The API (contract — build exactly against this)

### GET `/health`
→ `{"claude_cli": true}` — whether the local Claude CLI extractor is available.

### GET `/seed`
→ A JSON array of ~152 seed rows (MIT 18.01 calculus 2005+2006, 18.02 multivariable
2007, 18.06 linear algebra 2010), same shape as above. Load these on startup to have a
working demo with zero configuration. Four courses; 18.01 is the only one with two
years, which is where trend comparison is visible.

### POST `/pdftotext`
Request body: **raw PDF bytes** (`Content-Type: application/pdf`, just `fetch` with the
file's `BufferSource` — no multipart).
→ `200 {"text": "…extracted text…"}` or
`422 {"error": "No text layer found — this PDF looks like a scan. Paste the question text instead…"}`

### POST `/extract`  — turns one paper's text into question rows
Request:
```json
{
  "text": "<full text of one exam paper>",
  "course": "18.01",
  "year": 2007,
  "paper": "exam1",
  "existing_topics": ["tangent-lines", "implicit-differentiation"],
  "provider": "claude",              // "claude" | "openai" — optional, server picks a default
  "base_url": "https://api.openai.com/v1",  // only used when provider = "openai"
  "model": "gpt-4o-mini",            // only used when provider = "openai"
  "api_key": "sk-…"                  // only used when provider = "openai"
}
```
`existing_topics` is optional but IMPORTANT when adding a year to an existing course —
it keeps topic labels consistent so trends compute correctly. Send the union of topics
already present for that course.
→ `{"rows": [ <row>, … ]}` — rows have `course`/`year`/`paper` filled in from the request.
Errors come as `{"error": "<message>"}` with a 4xx/5xx status — surface the message
verbatim in the UI; it's written to be user-readable (e.g. "claude CLI not found…").

Two provider paths exist and must both be selectable in the UI:
1. **"Claude (this machine)"** — backend shells out to the local `claude` CLI. No key.
2. **"OpenAI-compatible"** — user supplies base URL + model + API key in a settings
   panel (stored in localStorage, sent per-request as above). Works with any
   OpenAI-compatible endpoint (OpenAI, OpenRouter, local servers).

### POST `/analyze`  — the trend engine (no LLM involved, always fast and free)
Request: `{"rows": [ <all rows — seed + user-added> ]}`
Response:
```json
{
  "courses": {
    "18.01": {
      "years": [2005, 2006],
      "paper_count": 10,
      "question_count": 70,
      "topics": [
        {
          "topic": "integration-by-parts",
          "label": "Integration by Parts",        // humanized for display
          "by_year": {"2005": {"count": 2, "marks": 30}, "2006": {"count": 1, "marks": 15}},
          "trend": "staple",                       // see trend values below
          "base": 22.5,                            // recency-weighted avg marks/yr
          "score": 22.5,                           // base × trend multiplier
          "evidence": "Tested every year (2005, 2006)",
          "questions": [ {row}, … ]                // every question in this topic
        }
      ],                                           // sorted by score desc
      "predictions": [
        {
          "rank": 1,
          "topic": "trigonometric-integrals-substitution",
          "label": "Trigonometric Integrals & Substitution",
          "trend": "staple",
          "expected_marks": 40,                    // rounded base
          "expected_questions": 3,                 // questions in most recent tested year
          "score": 40.0,
          "evidence": "Tested every year (2005, 2006)",
          "sample_questions": ["Evaluate ∫ sin^6 x cos x dx", "…"]  // up to 2 real questions
        }
      ]                                            // top 5 by score
    }
  }
}
```

**Trend values** (render each with a distinct chip; the label text is what matters —
do NOT use red/green status colors for them):
- `staple` — tested every single year
- `rising` — present every year and marks strictly increasing (needs 3+ years)
- `declining` — present every year but marks shrinking (needs 3+ years)
- `emerging` — absent in earlier years, tested recently
- `fading` — tested before, absent from the most recent year
- `alternating-due` — appears roughly every other year, wasn't tested last year → due
- `alternating-rest` — every-other-year pattern, was just tested → unlikely next
- `erratic` — no clear pattern
- `single-year` — only one year of data

**Scoring, for the "How it works" collapsible:** per topic, per year, a magnitude is
computed (marks when available, else question count). Years are recency-weighted
(0.7 per year back) and averaged into `base`. `score = base × multiplier` where the
multiplier is: staple 1.0, rising 1.3, emerging 1.15, declining 0.7, fading 0.5,
alternating-due 1.4, alternating-rest 0.4, erratic 0.7, single-year 0.8.

### POST `/predict`  — one-shot full pipeline (the demo money-shot)
Request:
```json
{
  "course": "my-school-physics",
  "papers": [
    {"year": 2024, "paper": "exam1", "text": "…"},
    {"year": 2025, "paper": "exam1", "text": "…"}
  ],
  "existing_rows": [ …optional seed/user rows to include in the analysis… ],
  "provider": "claude", "base_url": "…", "model": "…", "api_key": "…"   // same as /extract
}
```
→ `{"rows": [all rows, existing + newly extracted], "analysis": {<same shape as /analyze response>}}`
This endpoint extracts each paper then analyzes everything. Use it OR the
`/extract` + `/analyze` pair — your choice; the two-step pair gives you a preview
step between extraction and analysis.

## What to build (screens and flows)

**1. Header** — "ExamOracle" + tagline "Study-priority predictor — evidence-backed exam
forecasts from past papers". Settings gear (provider panel) top-right.

**2. Add-papers panel** (the pipeline — make this flow obvious and satisfying):
- Course selector: existing courses from loaded data, plus a "New course" option with a
  free-text id (slugify it).
- Year (number) and paper name (short text) inputs.
- File drop-zone accepting `.txt` (read client-side with `file.text()`) and `.pdf`
  (send raw bytes to `/pdftotext`; show the 422 scan message if returned).
- A paste-textarea as an always-works alternative.
- "Extract questions" button → calls `/extract` (with `existing_topics` if the course
  exists) → shows a **preview table of extracted rows** with per-row topic editable via
  a `<select>` of existing topics + free text → "Add to dataset" merges rows into state
  and localStorage, then re-runs the analysis.
- Show clear states: idle → extracting (spinner, "Reading paper… / Extracting questions…")
  → preview → done. Provider errors shown inline with the backend's message.

**3. Course dashboard** (tab per course; auto-select the course with most years):
- **KPI row** of 4 stat tiles: papers analyzed, years covered, questions extracted,
  topics identified.
- **Predictions panel** — the hero. Top-5 prediction cards, ranked: rank number, topic
  label, expected marks + question count, trend chip, one-line evidence, and 1–2 sample
  real questions from past papers (concrete proof the prediction is grounded).
- **Topic chart** — horizontal dumbbell chart of marks per topic, comparing the two most
  recent years: one dot per year (older = light shade, newer = dark shade of the same
  hue), thin connector line, topics sorted by score, top 12 topics. Include a two-dot
  legend with the year labels. Hover a topic row → tooltip with exact marks both years.
  If a course has only one year, render a plain horizontal bar chart of marks by topic
  instead. Clicking a topic scrolls to/expands its table row.
- **Topic table** — one row per topic: topic label · one column per year (marks,
  `tabular-nums`, em-dash when absent) · sparkline of marks across years (SVG, 2px
  stroke, dots when only 2 years) · trend chip · score · expand chevron revealing that
  topic's actual questions (year · paper · q · marks · text). Columns sortable.
- **"How scoring works"** collapsible footer with the scoring text above.

**4. Settings modal** — provider radio (Claude on this machine / OpenAI-compatible),
and for OpenAI-compatible: base URL, model, API key fields. Save to localStorage.
Show `/health` result so users can see if the Claude path is available.

## Design specs (follow these exactly)

System font stack: `system-ui, -apple-system, "Segoe UI", sans-serif`. Numbers in table
columns and axis ticks: `font-variant-numeric: tabular-nums`.

Colors via CSS custom properties, with full dark-mode support
(`@media (prefers-color-scheme: dark)`):

| Role | Light | Dark |
|---|---|---|
| Page background | `#f9f9f7` | `#0d0d0d` |
| Card surface | `#fcfcfb` | `#1a1a19` |
| Primary text | `#0b0b0b` | `#ffffff` |
| Secondary text | `#52514e` | `#c3c2b7` |
| Muted (axis/labels) | `#898781` | `#898781` |
| Grid/border hairline | `#e1e0d9` | `#2c2c2a` |
| Chart accent (sparkline, bars) | `#2a78d6` | `#3987e5` |
| Dumbbell: older year | `#86b6ef` | `#6da7ec` |
| Dumbbell: recent year | `#1c5cab` | `#3987e5` |

Chart rules: recessive hairline grid only (no heavy axes), one hue for the dumbbell
(two shades = two years, never two different hues), tooltip on hover for every mark,
2px connector lines, ≥8px dots with generous hover hit-areas. Trend chips are
hairline-bordered pills with secondary text color — identity by label, not by color.
No status colors anywhere. Cards: `border-radius: 8px`, hairline border, subtle shadow.

## Robustness / acceptance checklist

- [ ] Loads seed data via `/seed` and renders 18.01 (two years: dumbbell + trends) with
      no key, no provider, no user input.
- [ ] Can paste text for a new course+year, extract via the Claude provider (or a clear
      error if unavailable), preview rows, add, and see the analysis update.
- [ ] `.txt` upload works; `.pdf` upload works through `/pdftotext` when the server runs;
      a scanned PDF shows the friendly 422 message.
- [ ] Provider settings persist across reloads.
- [ ] Dark mode renders correctly (charts included).
- [ ] No console errors; no network calls except to the endpoints above.
- [ ] The whole story is demoable in under 90 seconds: seed dashboard → paste a paper →
      extract → predictions update.
