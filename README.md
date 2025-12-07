# HackNation – AI map guide

This project combines a FastAPI backend with a MapLibre front-end that loads
points of interest from semicolon-separated CSV files, renders them on a 3D map
of Bydgoszcz, and lets a local GGUF model (Gemma 2 27B Q4) narrate short
stories about each location. The map also supports a “mobile pose” mode that
uses device orientation to automatically trigger the narrator when a visitor
physically faces a monument.

The repository contains three relevant directories:

| Path | Description |
| ---- | ----------- |
| `FrontHackNation/` | Main front-end (MapLibre UI) and FastAPI backend (`main.py`). |
| `llm-html/` | Self-hosted WebLLM bundle (Wllama WASM, tokenizer assets, GGUF). |
| `pos-html/` | Prototype for the pose/orientation capture UI (parts merged into the front-end). |

## Requirements

- Python 3.10+ (tested with 3.11)
- `pip` / `venv`
- Optional: `node` + `npx` (only if you prefer `npx serve` for static hosting)
- MapTiler API key (replace the placeholder inside `FrontHackNation/index.html`)
- Adequate disk space for the GGUF model (~1.6 GB uncompressed)

## Quick start (local development)

1. **Install Python dependencies**
   ```bash
   cd FrontHackNation
   python3 -m venv .venv
   source .venv/bin/activate
   pip install fastapi uvicorn pydantic
   ```
   (If you have a `requirements.txt`, install from it instead.)

2. **Run the backend (serves API + LLM assets)**
   ```bash
   uvicorn FrontHackNation.main:app --host 0.0.0.0 --port 8000 --reload
   ```
   Endpoints exposed:
   - `GET /points` – merged data from `lokalzacja.csv` + `database.csv`
   - `GET /route-geojson` – walking route calculated via OSRM
   - `GET /lokalzacja.csv` / `GET /database.csv` – raw CSV fallback
   - `/llm-html/*` – static WebLLM bundle and `gemma-3-270m-it.Q4_K_M.gguf`

3. **Serve the front-end**
   The HTML file makes relative requests to `http://<host>:8000`, so any static
   server works. Example with Python:
   ```bash
   # from repo root
   python3 -m http.server 4173 --directory FrontHackNation
   ```
   Then open `http://localhost:4173/index.html`. If you host the front-end on a
   different port or domain, set `window.BACKEND_URL` before loading the map:
   ```html
   <script>window.BACKEND_URL = "https://your-domain.example/api";</script>
   ```

4. **Test the AI guide**
   - Allow the browser to fetch `/llm-html/esm/index.js` (should succeed if the
     static mount is configured correctly).
   - Click any marker or enable the mobile pose mode; the panel “Przewodnik AI”
     should display generated narratives within a few seconds.

## Working with the CSV data

- `FrontHackNation/lokalzacja.csv` – master list of POIs with coordinates.
- `FrontHackNation/database.csv` – richer descriptions (name, `Description`,
  optional `GPS ID` fallback).  
Update these files and restart the backend to rebuild the in-memory map and the
cached OSRM route.

## Deployment guide

1. **Prepare environment**
   - Copy the entire repository to your server (or deploy through CI).
   - Keep the `llm-html/` directory next to `FrontHackNation/main.py`, because
     `main.py` mounts it as `/llm-html` for the browser.
   - Replace the placeholder MapTiler key inside `FrontHackNation/index.html`.
   - Large model files (>100 MB) should use Git LFS or be uploaded manually
     (GitHub rejects large binaries otherwise).

2. **Install server dependencies**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install fastapi uvicorn "uvicorn[standard]" pydantic
   ```

3. **Run FastAPI under a process manager**
   ```bash
   uvicorn FrontHackNation.main:app --host 0.0.0.0 --port 8000
   ```
   or with Gunicorn:
   ```bash
   gunicorn -k uvicorn.workers.UvicornWorker FrontHackNation.main:app
   ```

4. **Serve the front-end**
   - Simple option: use the same FastAPI instance to serve the HTML by copying
     `FrontHackNation/index.html` into a static folder behind nginx/Apache.
   - Alternatively, deploy the HTML to a CDN/static host (Netlify, S3, etc.)
     and configure `window.BACKEND_URL` to point to your FastAPI base URL.

5. **Verify**
   - Visit the hosted HTML.
   - Check browser dev tools: `/llm-html/esm/index.js` and
     `/llm-html/gemma-3-270m-it.Q4_K_M.gguf` should load without 404/CORS errors.
   - Confirm `/points` and `/route-geojson` return JSON.
   - Click markers to ensure the Wllama worker streams answers.

## Troubleshooting

- **LLM fails with “invalid magic / typed array length”**  
  The OPFS cache contains a corrupted file; reload and the app will automatically
  clear the cache and retry. Ensure the full `.gguf` file deployed correctly.

- **Markers show outside Bydgoszcz or route is incorrect**  
  Verify `lokalzacja.csv` coordinates (the parser treats `x` as longitude,
  `y` as latitude) and restart the backend so OSRM recalculates the route.

- **Mobile pose mode not reacting**  
  Make sure the site is served over HTTPS when testing on iOS devices (required
  for `DeviceOrientationEvent.requestPermission`). Grant motion + location access.

---

Feel free to adapt these instructions for GitHub Pages, Docker, or any other
deployment target—just keep the FastAPI service, CSV files, and `llm-html`
bundle together so the map can reach both the data and the model.***
