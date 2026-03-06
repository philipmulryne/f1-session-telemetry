# F1 Session Telemetry Dashboard

Real-time F1 session telemetry analysis dashboard powered by OpenF1 API data with AI-powered insights (Gemini & Claude).

## Features

- **Live Session Data** — Browse any F1 race weekend (2023–2026), load FP1-3, Qualifying, Sprint, Race sessions
- **Lap Scatter Chart** — Visualize all laps with compound coloring, lasso selection, annotations
- **Speed Trap Rankings** — I1, I2, ST speed zone analysis per driver
- **Top Speed vs Mean Speed** — Team-level scatter with IQR outlier detection
- **Driving Phases** — Braking/Neutral/Partial/Max throttle breakdown per driver
- **Telemetry Comparison** — Overlay speed, throttle, brake, gear, delta for any two laps
- **Cross-Session Comparison** — Compare laps across different sessions or years at the same circuit
- **AI Telemetry Insights** — Gemini or Claude AI analysis of sessions, lap comparisons, practice data
- **Q1/Q2/Q3 Segmentation** — Auto-detect qualifying phases
- **Long Run Analysis** — Identify stints, compute degradation rates
- **Multi-Driver Lap-by-Lap** — Overlay lap time progression for selected drivers
- **Live Auto-Refresh** — 15s polling for active sessions
- **Historical Results** — Save and track season results with driver/team stats

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/f1-session-telemetry.git
cd f1-session-telemetry

# Install dependencies
pip install -r requirements.txt

# Set API keys (at least one for AI features)
export GEMINI_API_KEY=your_key_here
# export CLAUDE_API_KEY=your_key_here

# Run
python app.py
```

Open http://localhost:5000

## Deployment (Render)

1. Push to GitHub
2. Go to [render.com](https://render.com) → New Web Service → Connect your repo
3. Render will auto-detect `render.yaml` configuration
4. Add environment variables: `GEMINI_API_KEY` and/or `CLAUDE_API_KEY`
5. Deploy

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | For AI | Google Gemini API key ([get one free](https://aistudio.google.com/apikey)) |
| `CLAUDE_API_KEY` | For AI | Anthropic Claude API key |
| `PORT` | No | Server port (default: 5000) |
| `FLASK_DEBUG` | No | Set to `1` for debug mode |

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/season/meetings` | GET | List race weekends for a year |
| `/api/season/sessions` | GET | List sessions for a meeting |
| `/api/season/session_data` | GET | Fetch drivers, laps, stints |
| `/api/test_telemetry/car_data` | GET | Car telemetry (speed/throttle/brake/gear) |
| `/api/test_telemetry/location` | GET | Car location (x/y/z) |
| `/api/telemetry/analyze` | POST | AI session analysis |
| `/api/telemetry/compare_laps` | POST | AI lap comparison |
| `/api/telemetry/chat` | POST | AI follow-up chat |
| `/api/telemetry/practice_analysis` | POST | AI practice analysis |
| `/api/notes/from_ai_insight` | POST | Save AI insight as note |
| `/api/historical/save_session` | POST | Save session results |
| `/api/historical/stats` | GET | Aggregated season stats |

## Data Sources

- **OpenF1 API** — Live and historical F1 telemetry data (free, no key required for basic access)
- **Gemini API** — Google's AI for telemetry analysis (free tier available)
- **Claude API** — Anthropic's AI as alternative provider

## License

MIT
