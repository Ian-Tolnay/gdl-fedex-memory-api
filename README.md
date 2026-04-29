# GDL FedEx Memory Gateway API — Starter v0.1

This is a safe starter backend for the FedEx In-House Engineer MK2 memory system.

It uses:

- FastAPI on Render
- Airtable as the structured memory database
- A strict Custom GPT Action schema
- Draft/review memory records
- Dense AI-readable memory lines
- Semantic capsules
- Basic graph-edge extraction

## Why this replaces the old Google Sheets prototype

The old prototype used Google Sheets as the database and exposed broad sheet operations such as header updates, row passthrough, rename, and delete. That made the system flexible but fragile. This version uses strict API envelopes and keeps schema changes out of the Custom GPT action surface.

## Environment variables

Set these in Render:

```bash
AIRTABLE_TOKEN=pat_xxxxxxxxxxxxxxxxx
AIRTABLE_BASE_ID=appxxxxxxxxxxxxxx
MEMORY_API_KEY=choose-a-long-random-secret
```

## Airtable tables required

Create these tables with the field names in `airtable_setup.md`:

- Projects
- Memory_Records
- Sessions
- Tasks
- Issues
- Sources
- Validation_Results
- Shipping_Rules
- Graph_Edges

## Local run

```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell/CMD may vary
pip install -r requirements.txt
copy .env.example .env
uvicorn main:app --reload
```

Visit:

```text
http://127.0.0.1:8000/health
```

## Render start command

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Custom GPT Action

After deploying to Render, edit `openapi.actions.yaml` and replace:

```text
https://YOUR-RENDER-SERVICE.onrender.com
```

with your real Render URL.

Then paste/import that schema into the Custom GPT Actions editor and configure API key authentication as Bearer.

## First tests

Health:

```bash
curl https://YOUR-RENDER-SERVICE.onrender.com/health
```

Write memory:

```bash
curl -X POST https://YOUR-RENDER-SERVICE.onrender.com/memory/write \
  -H "Authorization: Bearer YOUR_MEMORY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id":"gdl-fedex-mk2",
    "record_type":"decision",
    "title":"Use Airtable for v0.1 memory",
    "raw_body":"We decided Airtable will be the v0.1 structured memory database, with Render as the controlled gateway API.",
    "priority":"high",
    "tags":["airtable","memory","architecture"]
  }'
```

Search memory:

```bash
curl -X POST https://YOUR-RENDER-SERVICE.onrender.com/memory/search \
  -H "Authorization: Bearer YOUR_MEMORY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"gdl-fedex-mk2","query":"Airtable memory architecture","limit":5}'
```
