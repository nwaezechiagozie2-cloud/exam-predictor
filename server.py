#!/usr/bin/env python3
"""ExamOracle local backend.

Serves the app and provides the API (contract in FRONTEND_PROMPT.md):

  GET  /health     -> {"claude_cli": bool}
  GET  /seed       -> JSON array of all rows from data/*.json
  POST /pdftotext  body = raw PDF bytes            -> {"text": "..."}
  POST /extract    body = extract contract payload -> {"rows": [...]}
      Paper content arrives as "text" and/or "images" (list of base64
      image bytes / data URLs — the LLM reads scanned pages itself, no OCR).
      Providers: "claude" (local CLI, no key) or "openai" (any
      OpenAI-compatible endpoint via base_url/model/api_key).
  POST /analyze    body = {"rows": [...]}          -> trend analysis
  POST /predict    body = papers + existing_rows   -> {"rows", "analysis"}

Run:  python3 server.py   (then open http://localhost:8787)
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
PORT = 8787

TREND_MULTIPLIERS = {
    "staple": 1.0,
    "rising": 1.3,
    "emerging": 1.15,
    "declining": 0.7,
    "fading": 0.5,
    "alternating-due": 1.4,
    "alternating-rest": 0.4,
    "erratic": 0.7,
    "single-year": 0.8,
}


class ExtractError(Exception):
    """User-readable extraction failure; `status` is the HTTP code."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status


# --- extraction ----------------------------------------------------------

def build_extraction_prompt(existing_topics):
    lines = [
        "You are extracting exam questions from a past exam paper. Read the paper "
        "below and return ONLY a JSON array — no commentary, no markdown fences — "
        "with one object per question:",
        '[{"q": "3", "topic": "implicit-differentiation", "marks": 15, '
        '"text": "Find dy/dx for y^4 + xy = 4"}]',
        "",
        "Rules:",
        '- q: the question number as printed. Split a question into separate rows '
        'ONLY when its parts test different topics; then use subpart ids like "3a", "3b".',
        "- marks: the question's point value (sum the subparts when split). "
        "Use null if the paper doesn't say.",
        "- topic: a short snake_case label for the topic being tested.",
        "- text: a summary of what is asked, 15 words or fewer.",
        "- Ignore instructions, boilerplate, cover pages and formula sheets.",
    ]
    if existing_topics:
        lines.append(
            "- IMPORTANT: reuse these existing topic labels when they fit "
            "(never invent a synonym): " + ", ".join(sorted(existing_topics)) +
            ". Only invent a new snake_case label if none of them fit.")
    else:
        lines.append(
            "- Keep labels general enough to recur across papers of the same "
            "course (e.g. integration-by-parts, optimization).")
    return "\n".join(lines)


def call_claude(prompt):
    if shutil.which("claude") is None:
        raise ExtractError(
            "claude CLI not found on PATH — log in with `claude` first, "
            "or switch the provider to OpenAI-compatible in settings.", 500)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise ExtractError("claude CLI timed out (10 min)", 504)
    if result.returncode != 0:
        raise ExtractError(
            f"claude CLI failed: {result.stderr.strip()[:500]}", 500)
    return result.stdout.strip()


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-20b"
# Set via environment: GROQ_API_KEY=gsk-... python3 server.py
# (never hardcode — GitHub push protection blocks secret commits)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


def call_openai(prompt, cfg):
    api_key = cfg.get("api_key") or GROQ_API_KEY
    if not api_key:
        raise ExtractError(
            "No API key configured — set GROQ_API_KEY when starting the "
            "server, or add a key in settings.", 400)
    base = (cfg.get("base_url") or GROQ_BASE_URL).rstrip("/")
    model = cfg.get("model") or GROQ_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8000,
    }
    # gpt-oss models: keep reasoning cheap so it doesn't eat the output budget
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"

    last_empty = False
    for attempt in range(3):
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}",
                     # Groq's Cloudflare rejects the default Python-urllib UA (error 1010)
                     "User-Agent": "ExamOracle/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                out = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 429 and attempt < 2:
                # Free-tier rate limit — wait the suggested time and retry
                wait = 5.0
                m = re.search(r"try again in ([\d.]+)s", detail)
                if m:
                    wait = min(float(m.group(1)) + 0.5, 15.0)
                time.sleep(wait)
                continue
            raise ExtractError(f"OpenAI-compatible API error {e.code}: {detail}", 502)
        except urllib.error.URLError as e:
            raise ExtractError(f"Could not reach {base}: {e.reason}", 502)
        try:
            message = out["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ExtractError("Unexpected response shape from the "
                               "OpenAI-compatible API", 502)
        content = (message.get("content") or "").strip()
        if not content:
            # gpt-oss sometimes leaves the answer only in the reasoning channel
            content = (message.get("reasoning") or "").strip()
        if content:
            return content
        last_empty = True  # empty reply — retry once more
    raise ExtractError(
        "The AI model returned an empty response"
        + (" — rate limited, wait a moment and try again" if last_empty else "")
        + ". Try again, or switch the provider to Claude in settings.", 502)


def call_llm(prompt, cfg):
    provider = cfg.get("provider") or "openai"
    if provider == "claude" and shutil.which("claude"):
        return call_claude(prompt)
    return call_openai(prompt, cfg)


def parse_rows(out, course, year, paper):
    s = out.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ExtractError(
            "Extractor did not return a JSON array — got: " + out[:300], 502)
    try:
        raw = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        raise ExtractError(
            "Extractor returned invalid JSON: " + out[:300], 502)
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        topic = str(r.get("topic") or "other").strip().lower()
        topic = re.sub(r"[^a-z0-9&:.-]+", "-", topic).strip("-") or "other"
        marks = r.get("marks")
        if isinstance(marks, bool) or not isinstance(marks, (int, float)):
            marks = None
        elif isinstance(marks, float) and marks.is_integer():
            marks = int(marks)
        rows.append({
            "course": course, "year": year, "paper": paper,
            "q": str(r.get("q") or "?"),
            "topic": topic,
            "marks": marks,
            "text": str(r.get("text") or "").strip(),
        })
    return rows


def extract_paper(text, course, year, paper, existing_topics, cfg):
    if not text or not text.strip():
        raise ExtractError("Exam paper text is empty", 400)
    prompt = build_extraction_prompt(existing_topics)
    prompt += "\n\n--- EXAM PAPER TEXT ---\n" + text.strip()
    out = call_llm(prompt, cfg)
    return parse_rows(out, course, year, paper)


# --- File / Document Text Extraction --------------------------------------

def extract_docx_text(data_bytes: bytes) -> str:
    try:
        with io.BytesIO(data_bytes) as bio:
            with zipfile.ZipFile(bio) as zf:
                xml_content = zf.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                paragraphs = []
                for p in tree.iter():
                    if p.tag.endswith("}p"):
                        texts = [node.text for node in p.iter() if node.tag.endswith("}t") and node.text]
                        if texts:
                            paragraphs.append("".join(texts))
                return "\n".join(paragraphs).strip()
    except Exception as e:
        raise ExtractError(f"Failed to extract text from DOCX: {e}", 422)


def extract_file_text(file_bytes: bytes) -> str:
    if not file_bytes:
        raise ExtractError("Empty file uploaded", 400)

    # Check for DOCX (Zip file containing word/document.xml)
    if file_bytes[:4] == b"PK\x03\x04":
        return extract_docx_text(file_bytes)

    # Check for PDF
    if file_bytes[:4] == b"%PDF":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", tmp_path, "-"],
                capture_output=True, text=True, timeout=60,
            )
            text = result.stdout.strip()
            if not text:
                raise ExtractError(
                    "No text layer found in this PDF (it may be a scanned image). "
                    "Please copy-paste the question text instead.", 422)
            return text
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # Plain text / DOC fallback
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return file_bytes.decode(encoding).strip()
        except UnicodeDecodeError:
            continue

    raise ExtractError("Could not decode document text", 422)


# --- analysis ------------------------------------------------------------

def humanize(topic):
    small = {"and", "of", "the", "by", "for", "to", "a", "an"}
    words = [w for w in topic.replace("other:", "").split("-") if w]
    return " ".join(w if w in small else w.capitalize() for w in words)


def classify_trend(n, mags, presence):
    if n == 1:
        return "single-year"
    if all(presence):
        if n >= 3 and all(mags[i] < mags[i + 1] for i in range(n - 1)):
            return "rising"
        if n >= 3 and all(mags[i] > mags[i + 1] for i in range(n - 1)):
            return "declining"
        return "staple"
    if n >= 3:
        idx = [i for i, p in enumerate(presence) if p]
        if len(idx) >= 2 and all(b - a > 1 for a, b in zip(idx, idx[1:])):
            return "alternating-rest" if presence[-1] else "alternating-due"
    if presence[-1]:
        return "emerging"
    return "fading"


def make_evidence(trend, years, present_years):
    yl = ", ".join(str(y) for y in years)
    pl = ", ".join(str(y) for y in present_years)
    last = present_years[-1] if present_years else (years[-1] if years else "unknown")
    if trend == "staple":
        return f"Tested every year ({yl})"
    if trend == "rising":
        return f"Tested every year, marks rising ({yl})"
    if trend == "declining":
        return f"Tested every year, marks declining ({yl})"
    if trend == "emerging":
        absent = ", ".join(str(y) for y in years if y not in present_years)
        return f"Absent in {absent}; tested {last}"
    if trend == "fading":
        return f"Not tested since {last}"
    if trend == "alternating-due":
        return f"Appears roughly every other year — last tested {last}, due next"
    if trend == "alternating-rest":
        return f"Appears roughly every other year — tested {last}, likely resting"
    if trend == "erratic":
        return f"No clear pattern (tested {pl})"
    return f"Only one year of data ({years[0] if years else 'unknown'})"  # single-year


def analyze(rows):
    grouped = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        course = str(r.get("course") or "").strip()
        if not course:
            continue
        grouped.setdefault(course, []).append(r)

    courses = {}
    for course, crows in grouped.items():
        year_set = set()
        for r in crows:
            try:
                year_set.add(int(r["year"]))
            except (TypeError, ValueError, KeyError):
                pass
        years = sorted(year_set)
        n = len(years)
        if n == 0:
            continue
        weights = [0.7 ** (n - 1 - i) for i in range(n)]
        paper_count = len({(int(r.get("year", 0)), str(r.get("paper") or ""))
                           for r in crows if str(r.get("year", "")).isdigit()})

        by_topic = {}
        for r in crows:
            by_topic.setdefault(str(r.get("topic") or "other"), []).append(r)

        topics_out = []
        for topic, trows in by_topic.items():
            by_year = {}
            mags = []
            for y in years:
                yr = [r for r in trows if str(r.get("year")) == str(y)]
                if yr:
                    mark_list = [r.get("marks") for r in yr
                                 if isinstance(r.get("marks"), (int, float))]
                    m = sum(mark_list) if mark_list else None
                    by_year[str(y)] = {"count": len(yr), "marks": m}
                    mags.append(m if (m or 0) > 0 else len(yr))
                else:
                    mags.append(0)

            base = sum(m * w for m, w in zip(mags, weights)) / sum(weights)
            presence = [m > 0 for m in mags]
            present_years = [y for y, p in zip(years, presence) if p]
            trend = classify_trend(n, mags, presence)
            score = base * TREND_MULTIPLIERS.get(trend, 1.0)
            topics_out.append({
                "topic": topic,
                "label": humanize(topic),
                "by_year": by_year,
                "trend": trend,
                "base": round(base, 1),
                "score": round(score, 1),
                "evidence": make_evidence(trend, years, present_years),
                "questions": sorted(
                    trows, key=lambda r: (int(r.get("year") or 0),
                                          str(r.get("paper") or ""),
                                          str(r.get("q") or ""))),
            })

        topics_out.sort(key=lambda t: -t["score"])

        predictions = []
        for rank, t in enumerate(topics_out[:5], 1):
            if not t["by_year"]:
                continue
            tested_years = [int(y) for y, d in t["by_year"].items() if d.get("count", 0) > 0]
            last_tested = max(tested_years) if tested_years else max(int(y) for y in t["by_year"])
            recent = t["by_year"].get(str(last_tested), {"count": 1})
            samples = [q.get("text", "") for q in t["questions"]
                       if str(q.get("year")) == str(last_tested) and q.get("text")][:2]
            for q in t["questions"]:
                if len(samples) >= 2:
                    break
                txt = q.get("text", "").strip()
                if txt and txt not in samples:
                    samples.append(txt)
            predictions.append({
                "rank": rank,
                "topic": t["topic"],
                "label": t["label"],
                "trend": t["trend"],
                "expected_marks": int(round(t["base"])),
                "expected_questions": max(1, recent.get("count", 1)),
                "score": t["score"],
                "evidence": t["evidence"],
                "sample_questions": samples,
            })

        courses[course] = {
            "years": years,
            "paper_count": paper_count,
            "question_count": len(crows),
            "topics": topics_out,
            "predictions": predictions,
        }
    return {"courses": courses}


# --- HTTP handler ---------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, ngrok-skip-browser-warning")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"claude_cli": shutil.which("claude") is not None})
        elif self.path == "/seed":
            self._handle_seed()
        else:
            super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if self.path in ("/pdftotext", "/filetotext"):
            self._handle_filetotext(body)
        elif self.path == "/extract":
            self._handle_extract(body)
        elif self.path == "/analyze":
            self._handle_analyze(body)
        elif self.path == "/predict":
            self._handle_predict(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_seed(self):
        rows = []
        for f in sorted(DATA_DIR.glob("*.json")):
            try:
                rows.extend(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        self._send_json(rows)

    def _handle_filetotext(self, file_bytes: bytes):
        try:
            text = extract_file_text(file_bytes)
            self._send_json({"text": text})
        except ExtractError as e:
            self._send_json({"error": e.message}, e.status)

    def _read_json(self, body):
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise json.JSONDecodeError("not an object", "", 0)
            return payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return None

    def _handle_extract(self, body: bytes):
        payload = self._read_json(body)
        if payload is None:
            return
        try:
            rows = self._extract_from_payload(payload, single_paper=True)
        except ExtractError as e:
            self._send_json({"error": e.message}, e.status)
            return
        self._send_json({"rows": rows})

    def _extract_from_payload(self, payload, single_paper):
        """Shared by /extract (one paper) and /predict (papers list)."""
        def get_paper(p):
            text = str(p.get("text") or "").strip()
            if not text:
                raise ExtractError("No exam paper text provided", 400)
            course = str(p.get("course") or "").strip()
            if not course:
                raise ExtractError("missing course", 400)
            try:
                year = int(p.get("year"))
            except (TypeError, ValueError):
                raise ExtractError("missing or invalid year", 400)
            paper = str(p.get("paper") or "paper").strip() or "paper"
            return text, course, year, paper

        if single_paper:
            text, course, year, paper = get_paper(payload)
            existing_topics = {str(t) for t in payload.get("existing_topics")
                               or [] if str(t).strip()}
            return extract_paper(text, course, year, paper,
                                 existing_topics, payload)

        # /predict: extract every paper, keeping topic labels consistent
        course = str(payload.get("course") or "").strip()
        if not course:
            raise ExtractError("missing course", 400)
        existing_rows = list(payload.get("existing_rows") or [])
        all_rows = [r for r in existing_rows]
        existing_topics = {str(r.get("topic")) for r in existing_rows
                           if str(r.get("course") or "") == course
                           and str(r.get("topic") or "").strip()}
        for p in payload.get("papers") or []:
            if not isinstance(p, dict):
                continue
            p = {**p, "course": course}
            text, c, year, paper = get_paper(p)
            rows = extract_paper(text, c, year, paper,
                                 existing_topics, payload)
            all_rows.extend(rows)
            existing_topics |= {r["topic"] for r in rows}
        return all_rows

    def _handle_analyze(self, body: bytes):
        payload = self._read_json(body)
        if payload is None:
            return
        rows = payload.get("rows")
        if not isinstance(rows, list):
            self._send_json({"error": "missing 'rows' array"}, 400)
            return
        self._send_json(analyze(rows))

    def _handle_predict(self, body: bytes):
        payload = self._read_json(body)
        if payload is None:
            return
        try:
            rows = self._extract_from_payload(payload, single_paper=False)
        except ExtractError as e:
            self._send_json({"error": e.message}, e.status)
            return
        self._send_json({"rows": rows, "analysis": analyze(rows)})


def main():
    if not APP_DIR.is_dir():
        APP_DIR.mkdir(parents=True, exist_ok=True)
    index = APP_DIR / "index.html"
    if not index.exists():
        index.write_text(
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>ExamOracle</title>"
            "<p style=\"font-family:system-ui;padding:2rem\">"
            "Frontend not built yet — run the frontend AI with "
            "FRONTEND_PROMPT.md; it writes app/index.html.</p>\n")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ExamOracle backend running at http://localhost:{PORT}")
    print(f"  claude CLI available: {shutil.which('claude') is not None}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
