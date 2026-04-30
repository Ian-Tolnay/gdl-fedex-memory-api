from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from airtable_client import AirtableError
from memory_service import MemoryService
from models import (
    ContextBuildRequest,
    FileCaptureRequest,
    MemorySearchRequest,
    MemoryWriteRequest,
    QuickCaptureRequest,
    RecordType,
    ValidationLogRequest,
    SessionCloseRequest,
)

app = FastAPI(
    title="GDL FedEx Memory Gateway API",
    version="0.1.0",
    description="Controlled memory gateway for Custom GPT project memory using Airtable as the structured store.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("MEMORY_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="MEMORY_API_KEY is not configured")
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def svc() -> MemoryService:
    try:
        return MemoryService()
    except AirtableError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "service": "gdl-fedex-memory-api"}


@app.get("/project/bootstrap", dependencies=[Depends(require_api_key)])
def project_bootstrap(project_id: str = Query(...)) -> Dict[str, Any]:
    try:
        return svc().project_bootstrap(project_id)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/memory/write", dependencies=[Depends(require_api_key)])
def memory_write(req: MemoryWriteRequest) -> Dict[str, Any]:
    try:
        return svc().write_memory(req)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/capture/quick", dependencies=[Depends(require_api_key)])
def capture_quick(req: QuickCaptureRequest) -> Dict[str, Any]:
    try:
        return svc().capture_quick(req)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/memory/search", dependencies=[Depends(require_api_key)])
def memory_search(req: MemorySearchRequest) -> Dict[str, Any]:
    try:
        return svc().search_memory(req)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/context/build", dependencies=[Depends(require_api_key)])
def context_build(req: ContextBuildRequest) -> Dict[str, Any]:
    try:
        return svc().build_context(req.project_id, req.query, req.token_budget, req.record_types, req.include_raw)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/capture/session", dependencies=[Depends(require_api_key)])
def capture_session(req: SessionCloseRequest) -> Dict[str, Any]:
    # v0.1 is synchronous for small batches. Move to a job queue before large imports.
    try:
        return svc().close_session(req)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/validation/log", dependencies=[Depends(require_api_key)])
def validation_log(req: ValidationLogRequest) -> Dict[str, Any]:
    try:
        return svc().log_validation(req)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/tasks/open", dependencies=[Depends(require_api_key)])
def tasks_open(project_id: str = Query(...)) -> Dict[str, Any]:
    try:
        return svc().open_tasks(project_id)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/issues/open", dependencies=[Depends(require_api_key)])
def issues_open(project_id: str = Query(...)) -> Dict[str, Any]:
    try:
        return svc().open_issues(project_id)
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/capture/files", dependencies=[Depends(require_api_key)])
def capture_files(req: FileCaptureRequest) -> Dict[str, Any]:
    # v0.1 stores file metadata as source_note memory. v0.2 can download/store/analyze files.
    body_lines = [req.note or "File capture from ChatGPT conversation."]
    for f in req.openaiFileIdRefs:
        body_lines.append(f"File: {f.name} | id={f.id} | mime={f.mime_type} | download_link_present={bool(f.download_link)}")
    mreq = MemoryWriteRequest(
        project_id=req.project_id,
        record_type=RecordType.source_note,
        title=f"Captured {len(req.openaiFileIdRefs)} file(s)",
        raw_body="\n".join(body_lines),
        human_summary=f"Captured {len(req.openaiFileIdRefs)} file(s) as source evidence.",
        tags=[*req.tags, "file_capture", "evidence"],
        metadata={"file_count": len(req.openaiFileIdRefs)},
    )
    try:
        result = svc().write_memory(mreq)
        return {"status": "captured_metadata", "file_count": len(req.openaiFileIdRefs), "memory_id": result["memory_id"]}
    except AirtableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/openapi.actions.yaml")
def openapi_actions_yaml() -> FileResponse:
    return FileResponse("openapi.actions.yaml", media_type="text/yaml")
