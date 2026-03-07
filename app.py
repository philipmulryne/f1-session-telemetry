"""
F1 Session Telemetry Dashboard — Standalone Flask App
Extracted from the full F1 Notes app for independent deployment.
Provides: session data (OpenF1), car telemetry, AI analysis (Gemini/Claude),
historical results tracking, and the session_telemetry.html frontend.
"""

from flask import Flask, request, jsonify, send_from_directory, redirect
import json
import time
import re
import os
import csv
import datetime
import datetime as dt
from io import StringIO
from collections import defaultdict

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOCS_DIR = os.path.join(STATIC_DIR, "docs")
DATA_DIR = os.path.join(BASE_DIR, "data")
_HIST_DIR = os.path.join(STATIC_DIR, "data", "historical")
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")

os.makedirs(DOCS_DIR, exist_ok=True)
os.makedirs(_HIST_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Create notes.json if it doesn't exist
if not os.path.exists(NOTES_PATH):
    with open(NOTES_PATH, "w") as f:
        json.dump({"notes": []}, f)

_APP_CONFIG_PATH = os.path.join(BASE_DIR, ".app_config.json")

def _load_app_config():
    if os.path.exists(_APP_CONFIG_PATH):
        with open(_APP_CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}

def _save_app_config(cfg):
    with open(_APP_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def _get_gemini_key():
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    return _load_app_config().get("gemini_api_key", "")

def _get_claude_key():
    key = os.environ.get("CLAUDE_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    return _load_app_config().get("claude_api_key", "")


# ══════════════════════════════════════════════════════════════════════════════
# OPENF1 AUTHENTICATION
# ══════════════════════════════════════════════════════════════════════════════
_OPENF1_TOKEN = None
_OPENF1_CREDS = None

def _openf1_obtain_token(username, password):
    import requests as _req
    try:
        r = _req.post("https://api.openf1.org/token",
                       data={"username": username, "password": password},
                       headers={"Content-Type": "application/x-www-form-urlencoded"},
                       timeout=15)
        if r.status_code != 200:
            return None, f"OpenF1 auth failed ({r.status_code}): {r.text[:200]}"
        tok = r.json()
        expires_in = int(tok.get("expires_in", 3600))
        return {
            "access_token": tok["access_token"],
            "expires_at": time.time() + expires_in - 60,
            "expires_in": expires_in,
        }, None
    except Exception as e:
        return None, str(e)

def _openf1_ensure_token():
    global _OPENF1_TOKEN
    if _OPENF1_TOKEN and time.time() < _OPENF1_TOKEN["expires_at"]:
        return True
    if _OPENF1_CREDS:
        tok, err = _openf1_obtain_token(_OPENF1_CREDS["username"], _OPENF1_CREDS["password"])
        if tok:
            _OPENF1_TOKEN = tok
            return True
    return False

def _openf1_headers():
    _openf1_ensure_token()
    if _OPENF1_TOKEN and time.time() < _OPENF1_TOKEN["expires_at"]:
        return {"Authorization": f"Bearer {_OPENF1_TOKEN['access_token']}",
                "accept": "application/json"}
    return {"accept": "application/json"}

@app.route("/api/openf1_auth", methods=["POST"])
def openf1_auth():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required"}), 400
    tok, err = _openf1_obtain_token(username, password)
    if err:
        return jsonify({"ok": False, "error": err}), 401
    global _OPENF1_TOKEN, _OPENF1_CREDS
    _OPENF1_TOKEN = tok
    _OPENF1_CREDS = {"username": username, "password": password}
    return jsonify({"ok": True, "expires_in": tok["expires_in"]})

@app.route("/api/openf1_auth/refresh", methods=["POST"])
def openf1_auth_refresh():
    global _OPENF1_TOKEN
    if not _OPENF1_CREDS:
        return jsonify({"ok": False, "error": "No stored credentials. Please log in first."}), 401
    tok, err = _openf1_obtain_token(_OPENF1_CREDS["username"], _OPENF1_CREDS["password"])
    if err:
        return jsonify({"ok": False, "error": err}), 401
    _OPENF1_TOKEN = tok
    return jsonify({"ok": True, "expires_in": tok["expires_in"]})

@app.route("/api/openf1_auth", methods=["GET"])
def openf1_auth_status():
    if _OPENF1_TOKEN and time.time() < _OPENF1_TOKEN["expires_at"]:
        remaining = int(_OPENF1_TOKEN["expires_at"] - time.time())
        return jsonify({"authenticated": True, "expires_in": remaining, "has_creds": _OPENF1_CREDS is not None})
    return jsonify({"authenticated": False, "has_creds": _OPENF1_CREDS is not None})


# ══════════════════════════════════════════════════════════════════════════════
# SEASON DATA ROUTES (OpenF1 proxy with caching)
# ══════════════════════════════════════════════════════════════════════════════
_SEASON_CACHE = {}

@app.route("/api/season/meetings", methods=["GET"])
def season_meetings():
    import requests as _req
    year = int(request.args.get("year", dt.datetime.now().year))
    ck = f"meetings_{year}"
    cached = _SEASON_CACHE.get(ck)
    if cached and (time.time() - cached["ts"]) < 600:
        return jsonify(cached["data"])
    try:
        r = _req.get("https://api.openf1.org/v1/meetings",
                      params={"year": year}, headers=_openf1_headers(), timeout=12)
        meetings = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    meetings.sort(key=lambda m: m.get("date_start", ""))
    _SEASON_CACHE[ck] = {"ts": time.time(), "data": meetings}
    return jsonify(meetings)

@app.route("/api/season/sessions", methods=["GET"])
def season_sessions():
    import requests as _req
    mk = request.args.get("meeting_key", "").strip()
    if not mk:
        return jsonify({"error": "meeting_key required"}), 400
    ck = f"sessions_{mk}"
    cached = _SEASON_CACHE.get(ck)
    if cached and (time.time() - cached["ts"]) < 300:
        return jsonify(cached["data"])
    try:
        r = _req.get("https://api.openf1.org/v1/sessions",
                      params={"meeting_key": mk}, headers=_openf1_headers(), timeout=12)
        sessions = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    sessions.sort(key=lambda s: s.get("date_start", ""))
    _SEASON_CACHE[ck] = {"ts": time.time(), "data": sessions}
    return jsonify(sessions)

@app.route("/api/season/session_data", methods=["GET"])
def season_session_data():
    import requests as _req
    sk = request.args.get("session_key", "").strip()
    if not sk:
        return jsonify({"error": "session_key required"}), 400
    ck = f"sdata_{sk}"
    nocache = request.args.get("nocache", "").strip()
    cached = _SEASON_CACHE.get(ck)
    if not nocache and cached and (time.time() - cached["ts"]) < 300:
        return jsonify(cached["data"])
    OPENF1 = "https://api.openf1.org/v1"
    hdrs = _openf1_headers()
    drivers_map = {}
    laps = []
    stints = []
    race_control = []
    try:
        dr = _req.get(f"{OPENF1}/drivers", params={"session_key": sk}, headers=hdrs, timeout=10)
        for d in (dr.json() if dr.status_code == 200 else []):
            dn = d.get("driver_number")
            if dn and dn not in drivers_map:
                drivers_map[dn] = {
                    "number": dn,
                    "acronym": d.get("name_acronym", ""),
                    "full_name": d.get("full_name", ""),
                    "team": d.get("team_name", ""),
                    "team_colour": d.get("team_colour", ""),
                }
    except Exception:
        pass
    try:
        lr = _req.get(f"{OPENF1}/laps", params={"session_key": sk}, headers=hdrs, timeout=15)
        laps = lr.json() if lr.status_code == 200 else []
    except Exception:
        pass
    try:
        sr = _req.get(f"{OPENF1}/stints", params={"session_key": sk}, headers=hdrs, timeout=10)
        stints = sr.json() if sr.status_code == 200 else []
    except Exception:
        pass
    try:
        rc = _req.get(f"{OPENF1}/race_control", params={"session_key": sk}, headers=hdrs, timeout=10)
        race_control = rc.json() if rc.status_code == 200 else []
    except Exception:
        pass
    positions = []
    try:
        pr = _req.get(f"{OPENF1}/position", params={"session_key": sk}, headers=hdrs, timeout=15)
        positions = pr.json() if pr.status_code == 200 else []
    except Exception:
        pass
    result = {
        "session_key": int(sk), "drivers": drivers_map,
        "laps": laps if isinstance(laps, list) else [],
        "stints": stints if isinstance(stints, list) else [],
        "race_control": race_control if isinstance(race_control, list) else [],
        "positions": positions if isinstance(positions, list) else []
    }
    _SEASON_CACHE[ck] = {"ts": time.time(), "data": result}
    return jsonify(result)

@app.route("/api/season/race_control", methods=["GET"])
def season_race_control():
    import requests as _req
    sk = request.args.get("session_key", "").strip()
    if not sk:
        return jsonify({"error": "session_key required"}), 400
    OPENF1 = "https://api.openf1.org/v1"
    hdrs = _openf1_headers()
    params = {"session_key": sk}
    after = request.args.get("after", "").strip()
    if after:
        params["date>"] = after
    try:
        rc = _req.get(f"{OPENF1}/race_control", params=params, headers=hdrs, timeout=10)
        msgs = rc.json() if rc.status_code == 200 else []
    except Exception:
        msgs = []
    return jsonify({"messages": msgs if isinstance(msgs, list) else []})


# ══════════════════════════════════════════════════════════════════════════════
# CAR TELEMETRY DATA (OpenF1 proxy)
# ══════════════════════════════════════════════════════════════════════════════
_CAR_DATA_CACHE = {}

@app.route("/api/test_telemetry/car_data", methods=["GET"])
def test_car_data():
    import requests as _req
    sk = request.args.get("session_key", "").strip()
    dn = request.args.get("driver_number", "").strip()
    lap_start = request.args.get("lap_start", "").strip()
    lap_end = request.args.get("lap_end", "").strip()
    if not sk or not dn:
        return jsonify({"error": "session_key and driver_number required"}), 400
    cache_key = f"car_{sk}_{dn}_{lap_start}_{lap_end}"
    cached = _CAR_DATA_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < 600:
        return jsonify(cached["data"])
    params = {"session_key": sk, "driver_number": dn}
    if lap_start:
        params["date>"] = lap_start
    if lap_end:
        params["date<"] = lap_end
    try:
        r = _req.get("https://api.openf1.org/v1/car_data", params=params, headers=_openf1_headers(), timeout=20)
        if r.status_code != 200:
            return jsonify({"error": f"OpenF1 car_data error: {r.status_code}"}), 502
        data = r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    _CAR_DATA_CACHE[cache_key] = {"ts": time.time(), "data": data}
    return jsonify(data)

_LOCATION_CACHE = {}

@app.route("/api/test_telemetry/location", methods=["GET"])
def test_location():
    import requests as _req
    sk = request.args.get("session_key", "").strip()
    dn = request.args.get("driver_number", "").strip()
    lap_start = request.args.get("lap_start", "").strip()
    lap_end = request.args.get("lap_end", "").strip()
    if not sk or not dn:
        return jsonify({"error": "session_key and driver_number required"}), 400
    cache_key = f"loc_{sk}_{dn}_{lap_start}_{lap_end}"
    cached = _LOCATION_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < 600:
        return jsonify(cached["data"])
    params = {"session_key": sk, "driver_number": dn}
    if lap_start:
        params["date>"] = lap_start
    if lap_end:
        params["date<"] = lap_end
    try:
        r = _req.get("https://api.openf1.org/v1/location", params=params, headers=_openf1_headers(), timeout=20)
        if r.status_code != 200:
            return jsonify({"error": f"OpenF1 location error: {r.status_code}"}), 502
        data = r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    _LOCATION_CACHE[cache_key] = {"ts": time.time(), "data": data}
    return jsonify(data)


# ══════════════════════════════════════════════════════════════════════════════
# AI TELEMETRY ANALYSIS (Gemini + Claude)
# ══════════════════════════════════════════════════════════════════════════════

# --- PDF text extraction (for circuit context) ---
_PDF_TEXT_CACHE = {}

def _extract_pdf_text(filepath):
    try:
        import PyPDF2
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            pages = []
            for i, page in enumerate(reader.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    pages.append(f"[Page {i+1}]\n{txt.strip()}")
            return "\n\n".join(pages)
    except ImportError:
        return ""
    except Exception as e:
        return f"(Error extracting text: {e})"


# --- Circuit-Specific Context ---
_CIRCUIT_CONTEXT_CACHE = {}

def _build_circuit_context(circuit_name, year=2026):
    cache_key = f"{circuit_name}_{year}"
    cached = _CIRCUIT_CONTEXT_CACHE.get(cache_key)
    if cached and (time.time() - cached["time"]) < 300:
        return cached["text"]

    parts = []
    circuit_lower = (circuit_name or "").lower()

    if os.path.isdir(DOCS_DIR):
        for fn in os.listdir(DOCS_DIR):
            if not fn.lower().endswith(".pdf"):
                continue
            fn_lower = fn.lower()
            circuit_keywords = [circuit_lower]
            if "australia" in circuit_lower or "albert" in circuit_lower or "melbourne" in circuit_lower:
                circuit_keywords += ["australia", "australian", "melbourne", "albert park"]
            elif "bahrain" in circuit_lower or "sakhir" in circuit_lower:
                circuit_keywords += ["bahrain", "sakhir"]
            elif "jeddah" in circuit_lower or "saudi" in circuit_lower:
                circuit_keywords += ["jeddah", "saudi"]
            elif "monaco" in circuit_lower:
                circuit_keywords += ["monaco", "monte carlo"]
            elif "silverstone" in circuit_lower or "british" in circuit_lower:
                circuit_keywords += ["silverstone", "british"]
            elif "monza" in circuit_lower or "italian" in circuit_lower:
                circuit_keywords += ["monza", "italian"]
            elif "spa" in circuit_lower or "belgian" in circuit_lower:
                circuit_keywords += ["spa", "belgian"]

            if any(kw in fn_lower for kw in circuit_keywords):
                try:
                    text = _extract_pdf_text(os.path.join(DOCS_DIR, fn))
                    if len(text) > 100:
                        truncated = text[:6000] if len(text) > 6000 else text
                        parts.append(f"[From document: {fn}]\n{truncated}")
                except:
                    pass

    if os.path.isdir(DOCS_DIR):
        general_keywords = ["power unit", "energy limit", "track energy", "race director"]
        for fn in os.listdir(DOCS_DIR):
            fn_lower = fn.lower()
            if not fn_lower.endswith(".pdf"):
                continue
            if any(kw in fn_lower for kw in general_keywords):
                already = any(fn in p for p in parts)
                if not already:
                    try:
                        text = _extract_pdf_text(os.path.join(DOCS_DIR, fn))
                        if len(text) > 100:
                            truncated = text[:4000] if len(text) > 4000 else text
                            parts.append(f"[From document: {fn}]\n{truncated}")
                    except:
                        pass

    if parts:
        context = "=== CIRCUIT-SPECIFIC DOCUMENTS & REGULATIONS ===\n"
        context += "Use this information to provide circuit-aware analysis. Pay special attention to:\n"
        context += "- PU power reduction sectors and exceptions\n"
        context += "- MGUK reset zones\n"
        context += "- Battery deployment/harvesting strategies specific to this track layout\n"
        context += "- Any unique circuit rules or features\n\n"
        context += "\n\n".join(parts)
        context += "\n=== END CIRCUIT CONTEXT ==="
    else:
        context = ""

    _CIRCUIT_CONTEXT_CACHE[cache_key] = {"text": context, "time": time.time()}
    return context


# --- Prompt Templates ---
_GEMINI_TELEMETRY_PROMPT = """You are an expert F1 race engineer and data analyst. Analyze the following telemetry session data and provide strategic insights.

Session: {session_name} at {circuit}
Session Type: {session_type}
Date: {date}

--- TELEMETRY SUMMARY ---
{telemetry_summary}
--- END SUMMARY ---

Provide a comprehensive analysis in markdown format with these sections:

## Key Findings
- 3-5 bullet points highlighting the most important observations

## Pace Analysis
Analyze the lap times, identify who has genuine pace vs. who might be sandbagging or struggling

## Tyre Strategy Insights
Based on stint data and degradation patterns, what can we infer about tyre performance?

## Team/Driver Comparisons
Notable gaps between teammates, surprising performances, or concerning trends

## Predictions & Recommendations
Based on this data, what might we expect in the next session or race?

Be specific with numbers and driver names. Use technical F1 terminology. Keep the analysis concise but insightful."""


_GEMINI_LAP_COMPARISON_PROMPT = """You are an expert F1 race engineer analyzing lap-to-lap telemetry data. Compare these two laps and provide detailed technical insights.

Circuit: {circuit}
Session: {session_name} ({session_type})
Season: {year}

{circuit_context}

--- LAP COMPARISON DATA ---
{comparison_data}
--- END DATA ---

CRITICAL CONTEXT FOR PRACTICE SESSIONS:
If this is a practice session (FP1, FP2, FP3), consider these factors that affect lap comparisons:
- **Engine/PU modes**: Drivers may be running different power unit modes. One driver might be in a low engine mode (sandbagging/harvesting) while the other pushes harder. This is especially visible in:
  - Straight-line speed differences (lower mode = less deployment = slower on straights)
  - Throttle application patterns (lift-and-coast = energy saving)
  - Speed trap differentials between teammates (>3 km/h gap suggests different modes)
- **Fuel loads**: Different fuel loads dramatically affect lap times (~0.3s per 10kg). Practice laps may have very different fuel levels.
- **Programme differences**: One driver may be doing a setup baseline while the other does aero rakes or sensor runs.
- DO NOT draw definitive conclusions about driver skill from practice session comparisons without acknowledging these variables.

Provide a detailed technical analysis in markdown format:

## Summary
One paragraph summarizing the key difference between these two laps and who has the advantage overall.
If this is a practice session, explicitly note which differences might be explained by engine modes/fuel loads vs genuine pace.

## Corner-by-Corner Analysis
For each significant corner/turn mentioned in the data, explain:
- Who carries more speed through the corner
- Braking point differences (who brakes later/earlier)
- Throttle application differences
- Time gained/lost at each section

## Driving Style Comparison
- Braking technique differences (trail braking, threshold braking)
- Throttle modulation patterns
- Gear usage and shift points
- Overall aggression vs smoothness

{energy_section}

## Key Advantage Areas
- Where does Driver 1 gain time?
- Where does Driver 2 gain time?
- Which corners show the biggest delta?
- Flag any advantage that could be explained by engine mode rather than driving

## Recommendations
What could each driver learn from the other? Specific actionable advice for improving lap time.

Use precise technical F1 terminology. Reference specific corners by name/number. Be concise but insightful."""

_ENERGY_MANAGEMENT_SECTION = """## Energy Management Analysis (2026 Regulations)
Based on the throttle and speed patterns, analyze:
- **Battery Deployment Zones**: Where is each driver deploying electrical energy? (typically out of slow corners, acceleration zones)
- **Energy Harvesting Zones**: Where is regenerative braking occurring? (heavy braking zones, lift-and-coast areas)
- **Deployment Strategy Differences**: Who deploys more aggressively? Who conserves for later in the lap?
- **Lift-and-Coast Patterns**: Any evidence of fuel/energy saving through early throttle lift?
- **ERS Efficiency**: Based on speed differentials on straights, who appears to have better energy deployment timing?

Note: The 2026 regulations feature a more powerful MGU-K (350kW vs 120kW) and no MGU-H, making battery management crucial."""


_GEMINI_PRACTICE_PROMPT = """You are an expert F1 race engineer analyzing practice session data. Provide strategic insights for race preparation.

Circuit: {circuit}
Session: {session_name}
Season: {year}

{circuit_context}

IMPORTANT CONTEXT - F1 Team Tiers:
- **Front-runners (Tier 1)**: Mercedes, Red Bull, McLaren, Ferrari - these teams fight for wins and podiums
- **Midfield (Tier 2)**: Aston Martin, Alpine, Williams, RB (VCARB), Haas, Sauber/Kick Sauber - these teams fight amongst each other for points

When analyzing pace, always consider which tier the teams belong to. A midfield team being close to front-runners is significant. Gaps within the midfield battle are also important.

--- PRACTICE SESSION DATA ---
{practice_data}
--- END DATA ---

Provide a detailed analysis in markdown format:

## Session Overview
Brief summary of the session - who looked strong, any surprises, weather/track conditions if evident from data.

## Long Run Analysis
For each significant long run identified:
- **Tyre degradation rate** (seconds per lap) - is it high, manageable, or low?
- **Fuel-corrected pace** estimate (typically 0.1s/lap fuel burn-off)
- **Consistency** - how stable were the lap times?
- Compare long-run pace between drivers on same compound
- Identify which teams have the best race pace vs single-lap pace

## Engine Mode Analysis
Based on speed trap data and pace patterns:
{engine_mode_section}

## Tyre Strategy Insights
- Which compound showed the best balance of pace vs degradation?
- Predicted optimal race strategy (1-stop vs 2-stop likelihood)
- Any signs of graining, overheating, or blistering from pace drop-off patterns?

## Team-by-Team Breakdown
Group by tier and analyze:
**Front-runners**: How do Mercedes, Red Bull, McLaren, Ferrari compare?
**Midfield battle**: Who's leading the midfield? Any teams punching above their weight?

## Teammate Comparisons
For each team, compare the two drivers:
- Who has the edge in single-lap pace?
- Who has better long-run/race pace?
- Any signs of different setup directions or driving styles?

## Race Predictions
Based on this practice data:
- Expected front-runner order
- Midfield pecking order
- Potential surprise performers or strugglers
- Key battles to watch

Be specific with lap times and deltas. Use F1 terminology. Consider that practice sessions may include glory runs, setup experiments, and different fuel loads."""

_ENGINE_MODE_SECTION_PRE2026 = """- Are any teams/drivers running lower engine modes (sandbagging)?
- Look for unusual speed trap differences between teammates (>3 km/h suggests different modes)
- Front-runners often hide pace in practice - factor this into predictions
- Consider if any team appears to be running higher modes than usual (pushing for data)"""

_ENGINE_MODE_SECTION_2026 = """- **Battery deployment patterns**: Any evidence of teams testing different deployment strategies?
- Speed trap variations may indicate different energy deployment modes
- With the 350kW MGU-K, battery management is crucial - look for lift-and-coast patterns
- Compare straight-line speed between teammates - large gaps may indicate different energy strategies
- Note: 2026 cars have no MGU-H, so harvesting only occurs under braking"""


# --- Gemini API call ---
def _call_gemini_telemetry(api_key, session_info, telemetry_summary):
    import urllib.request as urlreq
    import urllib.error

    prompt = _GEMINI_TELEMETRY_PROMPT.format(
        session_name=session_info.get("session_name", "Unknown"),
        circuit=session_info.get("circuit", "Unknown"),
        session_type=session_info.get("session_type", "Unknown"),
        date=session_info.get("date", "Unknown"),
        telemetry_summary=telemetry_summary
    )

    parts = [{"text": prompt}]
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    last_err = None

    for model in models:
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            try:
                req = urlreq.Request(api_url, data=payload_bytes, headers={"Content-Type": "application/json"})
                with urlreq.urlopen(req, timeout=90) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
            except urllib.error.HTTPError as he:
                last_err = he
                if he.code == 429:
                    time.sleep((attempt + 1) * 3)
                    continue
                elif he.code == 404:
                    break
                else:
                    raise
            except Exception as ex:
                last_err = ex
                if attempt < 2:
                    time.sleep(2)
                    continue
                break

    raise last_err or Exception("All Gemini models failed")


# --- Claude API calls ---
def _get_claude_session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["POST"])
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    return s

def _call_claude_for_prompt(api_key, prompt, max_tokens=8192, temperature=0.7):
    import requests
    session = _get_claude_session()
    last_err = None
    for attempt in range(3):
        try:
            resp = session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            if resp.status_code != 200:
                raise Exception(f"Claude API error: {resp.status_code} - {resp.text[:200]}")
            result = resp.json()
            return result.get("content", [{}])[0].get("text", "No response").strip()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ConnectionResetError, OSError) as e:
            last_err = e
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
                continue
        except Exception as e:
            raise
    raise Exception(f"Claude API connection failed after 3 attempts: {last_err}")

def _call_claude_chat(api_key, system_prompt, messages, max_tokens=4096, temperature=0.7):
    import requests
    session = _get_claude_session()
    last_err = None
    for attempt in range(3):
        try:
            resp = session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": messages,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                raise Exception(f"Claude API error: {resp.status_code} - {resp.text[:200]}")
            result = resp.json()
            return result.get("content", [{}])[0].get("text", "No response").strip()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ConnectionResetError, OSError) as e:
            last_err = e
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
                continue
        except Exception as e:
            raise
    raise Exception(f"Claude API connection failed after 3 attempts: {last_err}")


def _get_telemetry_api_key(provider):
    if provider == "claude":
        key = _get_claude_key()
        if not key:
            return None, "Claude API key not configured. Set CLAUDE_API_KEY env var."
        return key, None
    else:
        key = _get_gemini_key()
        if not key:
            return None, "Gemini API key not configured. Set GEMINI_API_KEY env var."
        return key, None


# --- Telemetry AI Routes ---
@app.route("/api/telemetry/analyze", methods=["POST"])
def telemetry_analyze():
    try:
        data = request.get_json(force=True)
        provider = (data.get("provider") or "gemini").strip().lower()
        session_info = data.get("session_info", {})
        telemetry_summary = data.get("summary", "")

        if not telemetry_summary or len(telemetry_summary) < 50:
            return jsonify({"error": "Insufficient telemetry data provided"}), 400

        api_key, err = _get_telemetry_api_key(provider)
        if err:
            return jsonify({"error": "NO_KEY", "message": err}), 400

        if provider == "claude":
            prompt = _GEMINI_TELEMETRY_PROMPT.format(
                session_name=session_info.get("session_name", "Unknown"),
                circuit=session_info.get("circuit", "Unknown"),
                session_type=session_info.get("session_type", "Unknown"),
                date=session_info.get("date", "Unknown"),
                telemetry_summary=telemetry_summary
            )
            analysis = _call_claude_for_prompt(api_key, prompt)
        else:
            analysis = _call_gemini_telemetry(api_key, session_info, telemetry_summary)

        return jsonify({"ok": True, "analysis": analysis, "provider": provider})

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid request: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetry/compare_laps", methods=["POST"])
def telemetry_compare_laps():
    try:
        data = request.get_json(force=True)
        provider = (data.get("provider") or "gemini").strip().lower()
        session_info = data.get("session_info", {})
        comparison_data = data.get("comparison_data", "")

        if not comparison_data or len(comparison_data) < 100:
            return jsonify({"error": "Insufficient comparison data provided"}), 400

        api_key, err = _get_telemetry_api_key(provider)
        if err:
            return jsonify({"error": "NO_KEY", "message": err}), 400

        year = session_info.get("year", 2025)
        try:
            year = int(year)
        except:
            year = 2025

        energy_section = _ENERGY_MANAGEMENT_SECTION if year >= 2026 else ""
        circuit_name = session_info.get("circuit", "Unknown")
        circuit_context = _build_circuit_context(circuit_name, year)

        prompt = _GEMINI_LAP_COMPARISON_PROMPT.format(
            circuit=circuit_name,
            session_name=session_info.get("session_name", "Unknown"),
            session_type=session_info.get("session_type", "Unknown"),
            year=year,
            comparison_data=comparison_data,
            energy_section=energy_section,
            circuit_context=circuit_context
        )

        if provider == "claude":
            content_text = _call_claude_for_prompt(api_key, prompt)
            return jsonify({"ok": True, "analysis": content_text, "provider": "claude"})

        # Gemini
        import urllib.request as urlreq
        import urllib.error

        parts = [{"text": prompt}]
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        models = ["gemini-2.5-flash", "gemini-2.0-flash"]
        last_err = None
        for model in models:
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            for attempt in range(3):
                try:
                    req = urlreq.Request(api_url, data=payload_bytes, headers={"Content-Type": "application/json"})
                    with urlreq.urlopen(req, timeout=90) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    content_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                    return jsonify({"ok": True, "analysis": content_text, "provider": "gemini"})
                except urlreq.HTTPError as he:
                    last_err = he
                    if he.code == 429:
                        time.sleep((attempt + 1) * 3)
                        continue
                    elif he.code == 404:
                        break
                    else:
                        raise
                except Exception as ex:
                    last_err = ex
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    break
        raise last_err or Exception("All Gemini models failed")

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid request: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetry/ers_zones", methods=["POST"])
def telemetry_ers_zones():
    """Analyze telemetry data to identify ERS clipping, super-clipping, and deployment zones."""
    try:
        data = request.get_json(force=True)
        telemetry_samples = data.get("telemetry_samples", [])
        driver_info = data.get("driver_info", "")
        circuit = data.get("circuit", "Unknown")

        if not telemetry_samples or len(telemetry_samples) < 10:
            return jsonify({"error": "Insufficient telemetry data"}), 400

        api_key = _get_claude_key()
        if not api_key:
            return jsonify({"error": "NO_KEY", "message": "Claude API key not configured"}), 400

        # Build a compact telemetry table for the prompt
        header = "pct | speed | throttle | brake | gear | rpm"
        rows = []
        for s in telemetry_samples:
            rows.append(f"{s.get('pct',0):.1f}% | {s.get('speed',0):.0f} | {s.get('throttle',0):.0f} | {s.get('brake',0):.0f} | {s.get('gear',0)} | {s.get('rpm',0):.0f}")
        telem_table = header + "\n" + "\n".join(rows)

        prompt = f"""You are an expert F1 telemetry engineer analyzing Energy Recovery System (ERS) behavior from car telemetry data.

Circuit: {circuit}
Driver: {driver_info}

Telemetry data (sampled across the lap):
{telem_table}

Analyze the speed, throttle, and RPM traces to identify zones where:

1. **DEPLOYMENT** — ERS deploying stored energy for extra power. Signs: speed increasing faster than expected at full throttle, RPM patterns showing electrical assist, strong acceleration on straights.

2. **CLIPPING** — MGU-K harvesting limiting available power. Signs: driver at 100% throttle but speed gain rate is notably lower than in deployment zones, RPM dipping or plateauing, slight power deficit visible.

3. **SUPER_CLIPPING** — Aggressive harvesting causing significant power loss. Signs: very noticeable speed deficit at full throttle, driver clearly power-limited, RPM significantly constrained.

Return ONLY valid JSON (no markdown, no code fences) in this exact format:
{{"zones": [
  {{"type": "DEPLOYMENT", "start_pct": 15.0, "end_pct": 22.0, "reason": "your explanation here"}},
  {{"type": "CLIPPING", "start_pct": 45.0, "end_pct": 52.0, "reason": "your explanation here"}}
]}}

Rules:
- start_pct and end_pct are percentages of lap distance (0-100)
- Only include zones you have reasonable confidence about
- Keep reasons concise (under 30 words each)
- Typically a lap has 2-5 deployment zones and 1-3 clipping zones
- If you cannot determine any zones, return {{"zones": []}}"""

        raw = _call_claude_for_prompt(api_key, prompt, max_tokens=2048, temperature=0.3)

        # Parse JSON from response (handle potential markdown wrapping)
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        zones = parsed.get("zones", [])

        return jsonify({"ok": True, "zones": zones})
    except json.JSONDecodeError:
        return jsonify({"ok": True, "zones": [], "warning": "Could not parse AI response"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetry/chat", methods=["POST"])
def telemetry_chat():
    try:
        data = request.get_json(force=True)
        provider = (data.get("provider") or "gemini").strip().lower()
        session_info = data.get("session_info", {})
        comparison_data = data.get("comparison_data", "")
        conversation = data.get("conversation", [])
        user_question = data.get("question", "")

        if not user_question:
            return jsonify({"error": "No question provided"}), 400

        api_key, err = _get_telemetry_api_key(provider)
        if err:
            return jsonify({"error": "NO_KEY", "message": err}), 400

        year = session_info.get("year", 2025)
        try:
            year = int(year)
        except:
            year = 2025

        circuit_name = session_info.get("circuit", "Unknown")
        circuit_context = _build_circuit_context(circuit_name, year)

        system_context = f"""You are an expert F1 race engineer having a conversation about telemetry data.
You have already analyzed a lap comparison and the user is asking follow-up questions.

Circuit: {circuit_name}
Session: {session_info.get("session_name", "Unknown")} ({session_info.get("session_type", "Unknown")})
Season: {year}

{circuit_context}

--- TELEMETRY CONTEXT ---
{comparison_data[:3000]}
--- END CONTEXT ---

{"Note: This is 2026+ regulations with the new 350kW MGU-K and no MGU-H. Energy management is crucial." if year >= 2026 else ""}

IMPORTANT: If this is a practice session, remember that drivers may be in different engine/PU modes. Speed differences on straights could be engine mode related, not pure driver skill. Always caveat accordingly.

Answer the user's question based on this telemetry data. Be specific, technical, and concise. Use F1 terminology."""

        if provider == "claude":
            claude_msgs = []
            for msg in conversation:
                role = "user" if msg.get("role") == "user" else "assistant"
                claude_msgs.append({"role": role, "content": msg.get("text", "")})
            claude_msgs.append({"role": "user", "content": user_question})
            content_text = _call_claude_chat(api_key, system_context, claude_msgs)
            return jsonify({"ok": True, "response": content_text, "provider": "claude"})

        # Gemini
        import urllib.request as urlreq
        import urllib.error

        contents = []
        contents.append({"role": "user", "parts": [{"text": system_context}]})
        contents.append({"role": "model", "parts": [{"text": "I understand. I'm ready to answer questions about this telemetry comparison."}]})
        for msg in conversation:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg.get("text", "")}]})
        contents.append({"role": "user", "parts": [{"text": user_question}]})

        payload = {
            "contents": contents,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048}
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        models = ["gemini-2.5-flash", "gemini-2.0-flash"]
        last_err = None
        for model in models:
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            for attempt in range(3):
                try:
                    req = urlreq.Request(api_url, data=payload_bytes, headers={"Content-Type": "application/json"})
                    with urlreq.urlopen(req, timeout=60) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    content_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                    return jsonify({"ok": True, "response": content_text, "provider": "gemini"})
                except urlreq.HTTPError as he:
                    last_err = he
                    if he.code == 429:
                        time.sleep((attempt + 1) * 2)
                        continue
                    elif he.code == 404:
                        break
                    else:
                        raise
                except Exception as ex:
                    last_err = ex
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    break
        raise last_err or Exception("All Gemini models failed")

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid request: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetry/practice_analysis", methods=["POST"])
def telemetry_practice_analysis():
    try:
        data = request.get_json(force=True)
        provider = (data.get("provider") or "gemini").strip().lower()
        session_info = data.get("session_info", {})
        practice_data = data.get("practice_data", "")

        if not practice_data or len(practice_data) < 100:
            return jsonify({"error": "Insufficient practice data provided"}), 400

        api_key, err = _get_telemetry_api_key(provider)
        if err:
            return jsonify({"error": "NO_KEY", "message": err}), 400

        year = session_info.get("year", 2025)
        try:
            year = int(year)
        except:
            year = 2025

        engine_mode_section = _ENGINE_MODE_SECTION_2026 if year >= 2026 else _ENGINE_MODE_SECTION_PRE2026
        circuit_name = session_info.get("circuit", "Unknown")
        circuit_context = _build_circuit_context(circuit_name, year)

        prompt = _GEMINI_PRACTICE_PROMPT.format(
            circuit=circuit_name,
            session_name=session_info.get("session_name", "Unknown"),
            year=year,
            practice_data=practice_data,
            engine_mode_section=engine_mode_section,
            circuit_context=circuit_context
        )

        if provider == "claude":
            content_text = _call_claude_for_prompt(api_key, prompt)
            return jsonify({"ok": True, "analysis": content_text, "provider": "claude"})

        # Gemini
        import urllib.request as urlreq
        import urllib.error

        parts = [{"text": prompt}]
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        models = ["gemini-2.5-flash", "gemini-2.0-flash"]
        last_err = None
        for model in models:
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            for attempt in range(3):
                try:
                    req = urlreq.Request(api_url, data=payload_bytes, headers={"Content-Type": "application/json"})
                    with urlreq.urlopen(req, timeout=90) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    content_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                    return jsonify({"ok": True, "analysis": content_text, "provider": "gemini"})
                except urlreq.HTTPError as he:
                    last_err = he
                    if he.code == 429:
                        time.sleep((attempt + 1) * 3)
                        continue
                    elif he.code == 404:
                        break
                    else:
                        raise
                except Exception as ex:
                    last_err = ex
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    break
        raise last_err or Exception("All Gemini models failed")

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid request: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# SAVE AI INSIGHT AS NOTE
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/notes/from_ai_insight", methods=["POST"])
def notes_from_ai_insight():
    data = request.get_json(force=True)
    analysis_text = (data.get("text") or "").strip()
    source = (data.get("source") or "AI Insight").strip()
    circuit = (data.get("circuit") or "").strip()
    session_name = (data.get("session") or "").strip()
    session_type = (data.get("session_type") or "").strip()
    year = (data.get("year") or "").strip()
    save = data.get("save", False)

    if not analysis_text:
        return jsonify({"error": "No analysis text provided"}), 400

    today = datetime.date.today().isoformat()

    title = source
    lines = analysis_text.split("\n")
    for line in lines:
        cleaned = line.strip().lstrip("#").strip()
        if cleaned and len(cleaned) > 5:
            title = cleaned[:120]
            break

    if circuit and session_name and title != source:
        title = f"{circuit} {session_name}: {title}"
    elif circuit and session_name:
        title = f"{circuit} {session_name} — AI Analysis"

    tags = ["ai_insight", "yapay_zeka_analizi"]
    if "telemetry" in source.lower() or "practice" in source.lower():
        tags.extend(["telemetry", "telemetri"])
    if circuit:
        circuit_tag = re.sub(r'[^a-z0-9]+', '_', circuit.lower()).strip('_')
        tags.append(circuit_tag)
    if session_type:
        tags.append(session_type.lower())
    if year:
        tags.append(year)

    content = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = stripped.lstrip("#").lstrip("- ").lstrip("* ").strip()
        if cleaned and len(cleaned) > 3:
            cleaned = re.sub(r'\*\*(.+?)\*\*', r'\1', cleaned)
            cleaned = re.sub(r'\*(.+?)\*', r'\1', cleaned)
            cleaned = re.sub(r'`(.+?)`', r'\1', cleaned)
            content.append(cleaned)

    discussion_points = [c for c in content if "?" in c][:5]

    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    note = {
        "content": content,
        "date": today,
        "discussion_points": discussion_points,
        "id": f"{slug}-{today}",
        "personnel": [],
        "tags": tags,
        "team": "F1",
        "title": title,
    }

    if save:
        try:
            with open(NOTES_PATH, "r", encoding="utf-8") as f:
                notes_data = json.load(f)
        except:
            notes_data = {"notes": []}
        if isinstance(notes_data, dict) and "notes" in notes_data:
            notes_data["notes"].insert(0, note)
        elif isinstance(notes_data, list):
            notes_data.insert(0, note)
        else:
            notes_data = {"notes": [note]}
        with open(NOTES_PATH, "w", encoding="utf-8") as f:
            json.dump(notes_data, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "note": note, "saved": save})


# ══════════════════════════════════════════════════════════════════════════════
# HISTORICAL RESULTS — CSV storage & stats API
# ══════════════════════════════════════════════════════════════════════════════
def _hist_csv_path(year, stype):
    return os.path.join(_HIST_DIR, f"{stype}_results_{year}.csv")

_RACE_POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
_SPRINT_POINTS = [8, 7, 6, 5, 4, 3, 2, 1]

def _f1_points(pos, is_sprint=False):
    tbl = _SPRINT_POINTS if is_sprint else _RACE_POINTS
    return tbl[pos - 1] if 1 <= pos <= len(tbl) else 0

_RACE_CSV_COLS = ["year","round","meeting_key","meeting_name","session_key","session_type",
    "driver_number","driver_acronym","team","grid_position","finish_position",
    "classified","status","points","fastest_lap","best_time","gap_to_leader",
    "total_time","laps_completed"]

_QUALI_CSV_COLS = ["year","round","meeting_key","meeting_name","session_key","session_type",
    "driver_number","driver_acronym","team","grid_position",
    "q1_time","q2_time","q3_time","best_time","eliminated_phase"]

_PRACTICE_CSV_COLS = ["year","round","meeting_key","meeting_name","session_key","session_name",
    "driver_number","driver_acronym","team","best_time","laps_completed",
    "position","gap_to_leader"]


@app.route("/api/historical/save_session", methods=["POST"])
def historical_save_session():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data"}), 400

    year = data.get("year", dt.datetime.now().year)
    stype = data.get("session_type", "race")
    meeting_key = data.get("meeting_key", "")
    meeting_name = data.get("meeting_name", "")
    session_key = data.get("session_key", "")
    round_num = data.get("round", 0)
    session_name = data.get("session_name", "")
    results = data.get("results", [])

    if not results:
        return jsonify({"error": "No results data"}), 400

    if stype in ("qualifying", "sprint_quali"):
        csv_type = "qualifying"
        cols = _QUALI_CSV_COLS
    elif stype in ("race", "sprint"):
        csv_type = stype
        cols = _RACE_CSV_COLS
    else:
        csv_type = "practice"
        cols = _PRACTICE_CSV_COLS

    csv_path = _hist_csv_path(year, csv_type)

    existing_rows = []
    if os.path.exists(csv_path):
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            existing_rows = [r for r in reader if str(r.get("session_key", "")) != str(session_key)]

    new_rows = []
    for r in results:
        row = {c: "" for c in cols}
        row["year"] = year
        row["round"] = round_num
        row["meeting_key"] = meeting_key
        row["meeting_name"] = meeting_name
        row["session_key"] = session_key
        row["session_type"] = stype
        if csv_type == "practice":
            row["session_name"] = session_name
        for k, v in r.items():
            if k in cols:
                row[k] = v
        new_rows.append(row)

    all_rows = existing_rows + new_rows
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    return jsonify({"status": "ok", "saved": len(new_rows), "file": os.path.basename(csv_path)})


@app.route("/api/historical/results")
def historical_results():
    year = request.args.get("year", str(dt.datetime.now().year))
    stype = request.args.get("type", "race")
    csv_path = _hist_csv_path(year, stype)
    if not os.path.exists(csv_path):
        return jsonify({"results": [], "year": year, "type": stype})
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return jsonify({"results": rows, "year": year, "type": stype})


@app.route("/api/historical/stats")
def historical_stats():
    year = request.args.get("year", str(dt.datetime.now().year))

    race_rows = []
    quali_rows = []
    sprint_rows = []
    for stype, container in [("race", race_rows), ("qualifying", quali_rows), ("sprint", sprint_rows)]:
        csv_path = _hist_csv_path(year, stype)
        if os.path.exists(csv_path):
            with open(csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                container.extend(list(reader))

    # --- Driver stats ---
    driver_stats = {}
    for r in race_rows:
        dn = r.get("driver_acronym", "?")
        if dn not in driver_stats:
            driver_stats[dn] = {"acronym": dn, "team": r.get("team", ""),
                "races": 0, "wins": 0, "podiums": 0, "points": 0,
                "dnfs": 0, "best_finish": 99, "grid_avg": 0, "finish_avg": 0,
                "front_rows": 0, "poles": 0, "fastest_laps": 0,
                "race_results": [], "grid_positions": []}
        ds = driver_stats[dn]
        ds["team"] = r.get("team", ds["team"])
        ds["races"] += 1
        fp = int(r.get("finish_position", 99) or 99)
        gp = int(r.get("grid_position", 99) or 99)
        pts_raw = r.get("points", "")
        is_sprint = r.get("session_type", "") == "sprint"
        pts = float(pts_raw) if pts_raw not in ("", None, "0") else _f1_points(fp, is_sprint)
        ds["points"] += pts
        if fp <= 3: ds["podiums"] += 1
        if fp == 1: ds["wins"] += 1
        if fp < ds["best_finish"]: ds["best_finish"] = fp
        if r.get("classified", "").lower() == "false" or r.get("status", "").lower() in ("dnf", "ret", "retired"):
            ds["dnfs"] += 1
        if r.get("fastest_lap", "").lower() == "true": ds["fastest_laps"] += 1
        ds["race_results"].append({"round": r.get("round", ""), "meeting": r.get("meeting_name", ""),
            "grid": gp, "finish": fp, "points": pts, "status": r.get("status", ""),
            "session_type": r.get("session_type", "race")})
        ds["grid_positions"].append(gp)
        ds["finish_avg"] = (ds["finish_avg"] * (ds["races"] - 1) + fp) / ds["races"]
        ds["grid_avg"] = (ds["grid_avg"] * (ds["races"] - 1) + gp) / ds["races"]

    for r in sprint_rows:
        dn = r.get("driver_acronym", "?")
        if dn not in driver_stats:
            driver_stats[dn] = {"acronym": dn, "team": r.get("team", ""),
                "races": 0, "wins": 0, "podiums": 0, "points": 0,
                "dnfs": 0, "best_finish": 99, "grid_avg": 0, "finish_avg": 0,
                "front_rows": 0, "poles": 0, "fastest_laps": 0,
                "race_results": [], "grid_positions": []}
        ds = driver_stats[dn]
        fp = int(r.get("finish_position", 99) or 99)
        pts_raw = r.get("points", "")
        pts = float(pts_raw) if pts_raw not in ("", None, "0") else _f1_points(fp, True)
        ds["points"] += pts

    for r in quali_rows:
        dn = r.get("driver_acronym", "?")
        if dn not in driver_stats:
            driver_stats[dn] = {"acronym": dn, "team": r.get("team", ""),
                "races": 0, "wins": 0, "podiums": 0, "points": 0,
                "dnfs": 0, "best_finish": 99, "grid_avg": 0, "finish_avg": 0,
                "front_rows": 0, "poles": 0, "fastest_laps": 0,
                "race_results": [], "grid_positions": []}
        ds = driver_stats[dn]
        gp = int(r.get("grid_position", 99) or 99)
        if gp == 1: ds["poles"] += 1
        if gp <= 2: ds["front_rows"] += 1

    # --- Team stats ---
    team_stats = {}
    team_round = defaultdict(lambda: defaultdict(list))
    for r in race_rows:
        team = r.get("team", "?")
        rd = r.get("round", "")
        team_round[team][rd].append(r)
        if team not in team_stats:
            team_stats[team] = {"team": team, "races": 0, "wins": 0, "podiums": 0,
                "points": 0, "one_twos": 0, "double_podiums": 0, "double_dnfs": 0,
                "front_row_lockouts": 0, "constructors_results": []}

    for team, rounds in team_round.items():
        ts = team_stats.get(team)
        if not ts: continue
        for rd, drivers in sorted(rounds.items()):
            positions = sorted([int(d.get("finish_position", 99) or 99) for d in drivers])
            pts = sum(float(d.get("points", "") or 0) if d.get("points", "") not in ("", None, "0") else _f1_points(int(d.get("finish_position", 99) or 99), d.get("session_type", "") == "sprint") for d in drivers)
            ts["points"] += pts
            ts["races"] += 1
            if positions and positions[0] == 1: ts["wins"] += 1
            podium_count = sum(1 for p in positions if p <= 3)
            if podium_count >= 2: ts["double_podiums"] += 1
            ts["podiums"] += podium_count
            if len(positions) >= 2 and positions[0] == 1 and positions[1] == 2: ts["one_twos"] += 1
            dnf_count = sum(1 for d in drivers if d.get("status", "").lower() in ("dnf", "ret", "retired"))
            if dnf_count >= 2: ts["double_dnfs"] += 1

    # --- Qualifying H2H ---
    quali_h2h = {}
    team_quali = defaultdict(lambda: defaultdict(list))
    for r in quali_rows:
        team = r.get("team", "?")
        rd = r.get("round", "")
        team_quali[team][rd].append(r)
    for team, rounds in team_quali.items():
        drivers_in_team = set()
        for rd, drvs in rounds.items():
            for d in drvs: drivers_in_team.add(d.get("driver_acronym", ""))
        if len(drivers_in_team) < 2: continue
        dlist = sorted(drivers_in_team)
        key = f"{dlist[0]} vs {dlist[1]}"
        h2h = {dlist[0]: 0, dlist[1]: 0, "rounds": []}
        for rd, drvs in sorted(rounds.items()):
            by_acr = {d.get("driver_acronym"): d for d in drvs}
            if dlist[0] in by_acr and dlist[1] in by_acr:
                g0 = int(by_acr[dlist[0]].get("grid_position", 99) or 99)
                g1 = int(by_acr[dlist[1]].get("grid_position", 99) or 99)
                if g0 < g1: h2h[dlist[0]] += 1
                elif g1 < g0: h2h[dlist[1]] += 1
                h2h["rounds"].append({"round": rd, "meeting": drvs[0].get("meeting_name", ""),
                    dlist[0]: g0, dlist[1]: g1})
        quali_h2h[key] = h2h

    return jsonify({
        "year": year,
        "driver_stats": driver_stats,
        "team_stats": team_stats,
        "quali_h2h": quali_h2h,
        "race_count": len(set(r.get("round") for r in race_rows)),
        "quali_count": len(set(r.get("round") for r in quali_rows))
    })


@app.route("/api/historical/years")
def historical_years():
    years = set()
    if os.path.exists(_HIST_DIR):
        for f in os.listdir(_HIST_DIR):
            if f.endswith(".csv"):
                parts = f.replace(".csv", "").split("_")
                if parts and parts[-1].isdigit():
                    years.add(int(parts[-1]))
    return jsonify({"years": sorted(years, reverse=True)})


# ══════════════════════════════════════════════════════════════════════════════
# FRONTEND ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return redirect("/dashboard")

@app.route("/dashboard")
@app.route("/f1notes/session_telemetry.html")
def session_telemetry():
    return send_from_directory(STATIC_DIR, "session_telemetry.html")


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": time.time()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
