# ExamOracle — Project Handoff

*Last updated: 2026-09-15 (backend COMPLETE except one image path — see "Backend status"). Read this fully before continuing work.*

## What this project is

**ExamOracle** — a study-priority predictor for a **2-hour school hackathon** (deadline:
the hackathon itself; build time remaining must be budgeted for that). Pipeline:

1. Upload/paste past exam papers (per course, per year)
2. Extract every question into structured rows via an LLM
3. Analyze topic frequency across years (trend detection)
4. Output ranked predictions for the next exam, each with evidence

Split of work: **this chat builds the backend API; a separate AI (given
`FRONTEND_PROMPT.md`) builds the frontend** into `app/index.html`.

## Directory layout

```
/home/chiagozie/exam-predictor/
├── server.py            # backend API (PARTIAL — see status below)
├── FRONTEND_PROMPT.md   # complete spec handed to the frontend AI
├── HANDOFF.md           # this file
├── data/                # extracted question rows (JSON), the seed dataset
│   ├── 18-01-2005.json  # 44 rows
│   ├── 18-01-2006.json  # 26 rows
│   ├── 18-02-2007.json  # 52 rows
│   └── 18-06-2010.json  # 30 rows     (152 rows total)
├── papers/              # downloaded MIT OCW exam PDFs + solutions (47 files, 4.9MB)
│   ├── 18-01-calculus/          # Fall 2006 exams 1–4 + sols + endofterm (not an exam)
│   ├── 18-01-calculus-2005/     # Fall 2005 exams 1–5 + sols + final
│   ├── 18-02-multivariable-2006/  # Spring 2006 exams — SCANNED IMAGES, unusable
│   ├── 18-02-multivariable-2007/  # Fall 2007 practice exams + sols (usable set)
│   ├── 18-06-linear-algebra-2010/  # exams 1–3 + final + sols
│   └── text/            # pdftotext -layout output (.txt per paper)
└── app/                 # (removed — frontend AI will create; server serves it)
```

Source of the papers: MIT OpenCourseWare (ocw.mit.edu), public, free.

## Backend status (server.py)

**ALL ENDPOINTS IMPLEMENTED AND TESTED** (2026-09-15):

- `GET /health` → `{"claude_cli": bool}` — tested, works.
- `GET /seed` → 152 rows from `data/*.json` — tested over HTTP.
- `POST /pdftotext` — tested with a real PDF (4499 chars out). Scanned PDFs
  → 422 with a friendly message that now also mentions uploading page images.
- `POST /extract` — UPGRADED TO CONTRACT. Takes `{"text", "course", "year",
  "paper", "existing_topics", "provider", "base_url", "model", "api_key"}`
  **plus `"images": [base64-or-data-URL, ...]`** (user-requested addition —
  LLM reads scanned pages itself, no OCR). Returns `{"rows": [...]}`.
  Tested end-to-end via the claude provider on a real 18.01 paper: 8 rows,
  topic labels stayed consistent with the taxonomy, one sensible novel label.
- `POST /analyze` — full trend algorithm (below), tested over HTTP and unit-
  tested: trend classifier passes all patterns (rising/declining/staple/
  emerging/fading/alternating-due/alternating-rest/single-year). Note: a
  topic present in years 1 and 4 only (gaps >1 everywhere) classifies as
  alternating — that matches the written spec, not a bug.
- `POST /predict` — tested end-to-end: extracts via claude, analyzes 3 years
  of 18.01; trends staple/declining/fading all appeared correctly.

**Extraction internals** (all in server.py, stdlib only):
- `build_extraction_prompt(existing_topics)` — the shared prompt rules (one
  row per problem, split subparts only when topics differ, q="3a", marks
  summed/null-if-unknown, snake_case, ≤15-word summaries, JSON array only,
  reuse existing topic labels when given).
- `decode_images()` — accepts base64 or data URLs → [(mime, bytes)].
- `call_claude(prompt, images)` — shells out to `claude -p ... --output-format
  text`. **ONE BROKEN PIECE: image attachment.** Verified experimentally with
  CLI v2.1.220: (a) passing image paths as positional args after the prompt —
  silently ignored, model sees no image; (b) piping PNG bytes via stdin —
  arrives as mangled text, unusable; (c) referencing a `/tmp` path in the
  prompt — model's Read tool lacks permission outside the working directory;
  (d) referencing a path INSIDE the working directory in the prompt — most
  promising, but the verification run hit a transient gateway 500 before
  confirming, then was interrupted. **FIX NEEDED: in `call_claude`, write the
  images into a temp dir under the project working directory** (e.g.
  `BASE_DIR/.tmp-img-<rand>/page1.png`, NOT /tmp) **and reference those paths
  in the prompt text** ("read the exam paper images at these paths"), clean up
  after. Then verify with a real image, e.g. convert a page of
  `papers/18-02-multivariable-2006/` (the image-only scans) to PNG and run
  /extract with images. (`--add-dir /tmp/...` may also work but the
  workdir-path approach was closest to confirmed.)
- `call_openai(prompt, images, cfg)` — POST `{base_url}/chat/completions`,
  Bearer key, images as base64 `image_url` content parts, urllib stdlib.
  **UNTESTED — no API key provided yet.** Error paths return readable
  messages (missing key, HTTP error + body excerpt, unreachable host).
- `parse_rows()` — strips markdown fences, extracts the JSON array, sanitizes
  topics to snake_case, coerces marks to int-or-null, fills course/year/paper
  from the request.
- `analyze(rows)` — pure function, implements the algorithm below exactly;
  `classify_trend`, `make_evidence`, `humanize` helpers.
- Provider default: request's `provider` field, else claude if the CLI exists,
  else openai. `main()` now auto-creates `app/` with a placeholder index.html
  if missing (was a hard sys.exit before).

**Known response-shape deviations from FRONTEND_PROMPT.md (both additive,
frontend must tolerate):**
- `/extract` and `/predict` additionally accept `"images"` per paper.
- `by_year` marks are `null` (not omitted) for years-with-data-but-no-marks.

## The row data model

```json
{"course": "18.01", "year": 2006, "paper": "exam1", "q": "3",
 "topic": "implicit-differentiation", "marks": 15,
 "text": "Find dy/dx for y defined implicitly by y^4 + xy = 4"}
```

Topic labels are canonical snake_case, shared across years of a course so trends
compute (18.01 taxonomy: tangent-lines, differentiation-rules, implicit-differentiation,
graphing-curve-sketching, optimization, related-rates, integration-substitution,
integration-by-parts, partial-fractions, trigonometric-integrals-substitution,
riemann-sums-numerical-integration, volumes-of-revolution, arc-length-surface-area,
polar-coordinates, fundamental-theorem-calculus, definite-integral-applications,
definition-of-derivative, limits, continuity-differentiability,
approximation-linearization, mean-value-theorem, differential-equations,
improper-integrals, infinite-series, taylor-series, lhospitals-rule, parametric-curves).
When extracting a paper for an EXISTING course, pass `existing_topics` in the prompt so
labels stay consistent.

## The analysis algorithm (for /analyze — not yet implemented)

Per course:
1. Aggregate rows: topic × year → `{count, marks}` (marks summed, null-marks excluded).
2. **Magnitude** per topic-year: `marks if marks > 0 else count`.
3. **Recency weights**: years sorted ascending, `w[i] = 0.7 ** (n-1-i)` (most recent = 1).
4. **base** = weighted average magnitude (absent year = 0).
5. **Trend classification** (presence = magnitude > 0):
   - n=1 → `single-year`
   - all years present: strictly increasing magnitudes (n≥3) → `rising`;
     strictly decreasing (n≥3) → `declining`; else `staple`
   - else if n≥3, ≥2 presence years, no two presence years adjacent →
     `alternating-due` (last year absent) / `alternating-rest` (last year present)
   - else last year present → `emerging`; last year absent → `fading`; fallback `erratic`
6. **Multipliers**: staple 1.0, rising 1.3, emerging 1.15, declining 0.7, fading 0.5,
   alternating-due 1.4, alternating-rest 0.4, erratic 0.7, single-year 0.8.
7. **score = base × multiplier**.
8. **Predictions**: top 5 by score. Each: rank, topic, humanized label, trend,
   expected_marks (round base), expected_questions (count in most recent tested year,
   min 1), score, evidence string (e.g. "Tested every year (2005, 2006)",
   "Not tested since 2005", "Appears roughly every other year — last tested 2005, due
   next"), sample_questions (up to 2 real `text` values from that topic).
9. Topics list sorted by score desc, each with by_year marks/count, trend, base, score,
   evidence, and all its question rows.

The user's core design insight (drives everything): objective vs theory questions behave
differently — objective coverage is broad (signal = density), theory slots are few
(signal = which topics get picked) — and topics can recur in year 1 while fading in
year 2, so yearly patterns (rising/fading/alternating) matter, not just raw frequency.
All current seed data happens to be theory-style MIT papers.

## Data caveats (from extraction agents — already handled, just be aware)

- 18.02-2007 rows are **practice exams** (real 2007 exams weren't posted; practice
  papers were that year's prep material). Some marks null.
- 18.01-2006 exam3 Q1 split rows have marks null (no per-part values in the PDF).
- 18.02-2007 has `other:stokes-theorem` (2 rows) and 18.06 has
  `other:incidence-matrix-networks` (1 row) — legitimate non-canonical labels.
- 18.02-2006 dataset was DROPPED: the PDFs are image-only scans, no tesseract on this
  machine, and a subagent burned a long time trying OCR workarounds (don't retry).
- 18.01-2005 marks sum to 100 per exam (verified). Final Q6 marks use 10 (per-problem
  statement) though the header table said 25.

## Frontend status

`FRONTEND_PROMPT.md` is finished and ready to hand to the frontend AI — it contains the
full API contract, data model, UI/screen specs, design colors (light+dark), and an
acceptance checklist. The frontend AI writes `/home/chiagozie/exam-predictor/app/index.html`
(single file, vanilla JS, no build step) which the backend serves at localhost:8787.

## Next steps, in order

1. **Fix claude-provider image attachment** (see "Backend status" — write images
   under the project working dir, reference paths in the prompt, verify with a
   scanned 18.02-2006 page).
2. Test the openai provider once the user supplies an API key (per-request
   fields, never hardcode).
3. Coordinate with the frontend AI's output (it builds `app/index.html` per
   FRONTEND_PROMPT.md; backend already serves it at localhost:8787, with a
   placeholder page if not built yet); demo rehearsal: seed dashboard → paste
   (or upload/image) a paper → extract → predictions update (<90 seconds).
4. When wiring the frontend, note the two additive deviations listed above.

## Environment notes

- Fedora Linux, bash. `pdftotext`, `python3`, `node` available. NO tesseract. `claude`
  CLI v2.1.220 works. No pip installs — user got annoyed by permission prompts from
  install attempts; stdlib only.
- Web access works via curl for most domains; `files.pythonhosted.org` downloads and
  `api.ocr.space` were denied by the permission system.
- Run backend: `cd /home/chiagozie/exam-predictor && python3 server.py` → port 8787.
