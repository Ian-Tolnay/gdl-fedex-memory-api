from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from airtable_client import AirtableClient, AirtableError, airtable_formula_equals
from memory_compiler import compile_memory, make_session_id, make_validation_id, utc_now
from models import (
    MemorySearchRequest,
    MemoryStatus,
    MemoryWriteRequest,
    Priority,
    QuickCaptureRequest,
    RecordType,
    ReviewStatus,
    SessionCloseRequest,
    SessionItem,
    ValidationLogRequest,
)

TABLES = {
    "projects": "Projects",
    "memory": "Memory_Records",
    "sessions": "Sessions",
    "tasks": "Tasks",
    "issues": "Issues",
    "sources": "Sources",
    "validation": "Validation_Results",
    "shipping_rules": "Shipping_Rules",
    "graph_edges": "Graph_Edges",
}

# Airtable field names expected in v0.1. Keep these stable.
FIELD_MAP = {
    "memory_id": "memory_id",
    "project_id": "project_id",
    "record_type": "record_type",
    "title": "title",
    "raw_body": "raw_body",
    "human_summary": "human_summary",
    "semantic_capsule": "semantic_capsule",
    "ai_dense_line": "ai_dense_line",
    "retrieval_hint": "retrieval_hint",
    "status": "status",
    "priority": "priority",
    "tags": "tags",
    "linked_record_ids": "linked_record_ids",
    "source_refs": "source_refs",
    "confidence": "confidence",
    "review_status": "review_status",
    "metadata_json": "metadata_json",
    "entity_triples_json": "entity_triples_json",
    "claims_json": "claims_json",
    "causal_links_json": "causal_links_json",
    "token_estimate_raw": "token_estimate_raw",
    "token_estimate_dense": "token_estimate_dense",
    "compression_ratio": "compression_ratio",
    "content_hash": "content_hash",
    "created_at": "created_at",
    "updated_at": "updated_at",
}


def to_airtable_fields(compiled: Dict[str, Any]) -> Dict[str, Any]:
    fields = {FIELD_MAP[k]: v for k, v in compiled.items() if k in FIELD_MAP}
    return fields


def project_formula(project_id: str) -> str:
    return airtable_formula_equals("project_id", project_id)


class MemoryService:
    def __init__(self, client: Optional[AirtableClient] = None):
        self.client = client or AirtableClient()

    def project_bootstrap(self, project_id: str) -> Dict[str, Any]:
        project_records = self.client.list_records(TABLES["projects"], formula=project_formula(project_id), max_records=1)
        project = project_records[0].get("fields", {}) if project_records else {}

        recent = self.client.list_records(
            TABLES["memory"],
            formula=f"AND({project_formula(project_id)}, NOT({{status}}='deprecated'))",
            max_records=12,
        )
        tasks = self.client.list_records(
            TABLES["tasks"],
            formula=f"AND({project_formula(project_id)}, OR({{status}}='open', {{status}}='in_progress', {{status}}='blocked'))",
            max_records=10,
        )
        issues = self.client.list_records(
            TABLES["issues"],
            formula=f"AND({project_formula(project_id)}, OR({{status}}='open', {{status}}='investigating'))",
            max_records=10,
        )
        return {
            "project_id": project_id,
            "project": project,
            "recent_dense_memory": [r.get("fields", {}).get("ai_dense_line", "") for r in recent if r.get("fields", {}).get("ai_dense_line")],
            "open_tasks": [r.get("fields", {}) for r in tasks],
            "open_issues": [r.get("fields", {}) for r in issues],
        }

    def write_memory(self, req: MemoryWriteRequest) -> Dict[str, Any]:
        compiled = compile_memory(req)
        record = self.client.create_records(TABLES["memory"], [to_airtable_fields(compiled)])[0]
        # Store graph edges as draft rows if any were generated.
        try:
            edges = json.loads(compiled.get("entity_triples_json", "[]"))
            if edges:
                edge_rows = []
                for edge in edges:
                    edge_rows.append({
                        "edge_id": edge.get("edge_id"),
                        "project_id": req.project_id,
                        "source_entity": edge.get("source_entity"),
                        "relationship": edge.get("relationship"),
                        "target_entity": edge.get("target_entity"),
                        "source_memory_id": compiled["memory_id"],
                        "confidence": edge.get("confidence", 0.45),
                        "status": edge.get("status", "draft"),
                        "created_at": compiled["created_at"],
                    })
                self.client.create_records(TABLES["graph_edges"], edge_rows)
        except Exception:
            # Graph edge failure must not block primary memory creation in v0.1.
            pass
        return {"memory_id": compiled["memory_id"], "airtable_record_id": record.get("id"), "compiled": compiled}

    def capture_quick(self, req: QuickCaptureRequest) -> Dict[str, Any]:
        """
        Quick capture wrapper.

        Creates a normal Memory_Records row, then mirrors task/issue captures
        into the dedicated dashboard tables.
        """
        title = req.title or req.text[:80]

        mreq = MemoryWriteRequest(
            project_id=req.project_id,
            record_type=req.capture_type,
            title=title,
            raw_body=req.text,
            human_summary=req.text[:500],
            priority=req.priority,
            tags=[*req.tags, req.capture_type.value],
            source_refs=[req.source_ref] if req.source_ref else [],
            metadata={"capture_mode": "quick_capture"},
            review_status=req.review_status,
        )

        result = self.write_memory(mreq)
        compiled = result.get("compiled", {})
        memory_id = result["memory_id"]
        created_at = compiled.get("created_at") or utc_now()

        mirrored_to: List[str] = []

        if req.capture_type == RecordType.task:
            self.client.create_records(TABLES["tasks"], [{
                "task_id": f"TASK-{memory_id}",
                "project_id": req.project_id,
                "title": title,
                "status": "open",
                "priority": req.priority.value,
                "owner": "",
                "due_date": "",
                "linked_memory_ids": memory_id,
                "notes": req.text,
                "created_at": created_at,
                "updated_at": created_at,
            }])
            mirrored_to.append("Tasks")

        elif req.capture_type == RecordType.issue:
            self.client.create_records(TABLES["issues"], [{
                "issue_id": f"ISS-{memory_id}",
                "project_id": req.project_id,
                "title": title,
                "severity": req.priority.value,
                "status": "open",
                "symptom": req.text,
                "root_cause": "",
                "workaround": "",
                "resolution": "",
                "linked_memory_ids": memory_id,
                "created_at": created_at,
                "updated_at": created_at,
            }])
            mirrored_to.append("Issues")

        result["mirrored_to"] = mirrored_to
        return result

    def search_memory(self, req: MemorySearchRequest) -> Dict[str, Any]:
        formulas = [project_formula(req.project_id)]
        if req.status:
            formulas.append(airtable_formula_equals("status", req.status.value))
        if req.record_types:
            type_formula = "OR(" + ", ".join(airtable_formula_equals("record_type", t.value) for t in req.record_types) + ")"
            formulas.append(type_formula)
        formula = "AND(" + ", ".join(formulas) + ")" if len(formulas) > 1 else formulas[0]
        records = self.client.list_records(TABLES["memory"], formula=formula, max_records=100)
        ranked = self._rank_records(req.query, records, req.tags)
        results = []
        for rec in ranked[: req.limit]:
            f = rec.get("fields", {})
            item = {
                "memory_id": f.get("memory_id"),
                "record_type": f.get("record_type"),
                "title": f.get("title"),
                "human_summary": f.get("human_summary"),
                "semantic_capsule": f.get("semantic_capsule"),
                "ai_dense_line": f.get("ai_dense_line"),
                "retrieval_hint": f.get("retrieval_hint"),
                "priority": f.get("priority"),
                "status": f.get("status"),
                "tags": f.get("tags"),
                "source_refs": f.get("source_refs"),
            }
            if req.include_raw:
                item["raw_body"] = f.get("raw_body")
            results.append(item)
        return {"query": req.query, "count": len(results), "results": results}

    def pending_reviews(self, project_id: str, limit: int = 25) -> Dict[str, Any]:
        formula = (
            f"AND("
            f"{project_formula(project_id)}, "
            f"OR({{review_status}}='pending_review', {{status}}='pending_review')"
            f")"
        )

        records = self.client.list_records(
            TABLES["memory"],
            formula=formula,
            max_records=limit,
        )

        results = []
        for rec in records:
            fields = rec.get("fields", {})
            results.append({
                "airtable_record_id": rec.get("id"),
                "memory_id": fields.get("memory_id"),
                "record_type": fields.get("record_type"),
                "title": fields.get("title"),
                "human_summary": fields.get("human_summary"),
                "semantic_capsule": fields.get("semantic_capsule"),
                "ai_dense_line": fields.get("ai_dense_line"),
                "status": fields.get("status"),
                "review_status": fields.get("review_status"),
                "priority": fields.get("priority"),
                "tags": fields.get("tags"),
                "created_at": fields.get("created_at"),
            })

        return {
            "project_id": project_id,
            "count": len(results),
            "pending_reviews": results,
        }

    def approve_memory(
        self,
        memory_id: str,
        reviewer: Optional[str] = None,
        review_note: Optional[str] = None,
        new_status: MemoryStatus = MemoryStatus.active,
    ) -> Dict[str, Any]:
        rec = self._find_memory_record(memory_id)
        fields = rec.get("fields", {})
        metadata_json = self._merge_review_metadata(
            fields=fields,
            action="approved",
            reviewer=reviewer,
            review_note=review_note,
        )

        updated = self.client.update_record(TABLES["memory"], rec["id"], {
            "status": new_status.value,
            "review_status": "reviewed",
            "metadata_json": metadata_json,
            "updated_at": utc_now(),
        })

        return {
            "memory_id": memory_id,
            "airtable_record_id": updated.get("id"),
            "status": new_status.value,
            "review_status": "reviewed",
            "message": "Memory approved",
        }

    def reject_memory(
        self,
        memory_id: str,
        reviewer: Optional[str] = None,
        review_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        rec = self._find_memory_record(memory_id)
        fields = rec.get("fields", {})
        metadata_json = self._merge_review_metadata(
            fields=fields,
            action="rejected",
            reviewer=reviewer,
            review_note=review_note,
        )

        updated = self.client.update_record(TABLES["memory"], rec["id"], {
            "status": "deprecated",
            "review_status": "rejected",
            "metadata_json": metadata_json,
            "updated_at": utc_now(),
        })

        return {
            "memory_id": memory_id,
            "airtable_record_id": updated.get("id"),
            "status": "deprecated",
            "review_status": "rejected",
            "message": "Memory rejected",
        }

    def bulk_review_memory(
        self,
        memory_ids: List[str],
        action: str,
        reviewer: Optional[str] = None,
        review_note: Optional[str] = None,
        new_status: MemoryStatus = MemoryStatus.active,
    ) -> Dict[str, Any]:
        """
        Approve or reject multiple memory records in one request.

        This reduces review friction and avoids approving/rejecting one record per prompt.
        """
        updated: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for memory_id in memory_ids:
            try:
                if action == "approve":
                    result = self.approve_memory(
                        memory_id=memory_id,
                        reviewer=reviewer,
                        review_note=review_note,
                        new_status=new_status,
                    )
                elif action == "reject":
                    result = self.reject_memory(
                        memory_id=memory_id,
                        reviewer=reviewer,
                        review_note=review_note,
                    )
                else:
                    raise ValueError(f"Unsupported bulk review action: {action}")

                updated.append(result)

            except Exception as exc:
                errors.append({
                    "memory_id": memory_id,
                    "error": str(exc),
                })

        return {
            "action": action,
            "requested_count": len(memory_ids),
            "updated_count": len(updated),
            "error_count": len(errors),
            "updated": updated,
            "errors": errors,
        }

    def _find_memory_record(self, memory_id: str) -> Dict[str, Any]:
        records = self.client.list_records(
            TABLES["memory"],
            formula=airtable_formula_equals("memory_id", memory_id),
            max_records=1,
        )

        if not records:
            raise AirtableError(f"No memory record found for memory_id={memory_id}")

        return records[0]

    def _merge_review_metadata(
        self,
        fields: Dict[str, Any],
        action: str,
        reviewer: Optional[str] = None,
        review_note: Optional[str] = None,
    ) -> str:
        raw_metadata = fields.get("metadata_json") or "{}"

        try:
            metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata)
        except Exception:
            metadata = {"previous_metadata_json": str(raw_metadata)}

        metadata["review"] = {
            "action": action,
            "reviewer": reviewer or "",
            "review_note": review_note or "",
            "reviewed_at": utc_now(),
        }

        return json.dumps(metadata, ensure_ascii=False)

    def build_context(self, project_id: str, query: str, token_budget: int, record_types: Optional[List[RecordType]] = None, include_raw: bool = False) -> Dict[str, Any]:
        search = self.search_memory(MemorySearchRequest(
            project_id=project_id,
            query=query,
            record_types=record_types or [],
            limit=25,
            include_raw=include_raw,
        ))
        lines: List[str] = [f"PROJECT={project_id}", f"QUERY={query}"]
        used = 0
        for item in search["results"]:
            text = item.get("raw_body") if include_raw else item.get("ai_dense_line") or item.get("semantic_capsule") or item.get("human_summary")
            if not text:
                continue
            cost = max(1, int(len(text) / 4))
            if used + cost > token_budget:
                break
            lines.append(text)
            used += cost
        return {"token_budget": token_budget, "estimated_tokens": used, "context_pack": "\n".join(lines), "source_memory_ids": [i.get("memory_id") for i in search["results"]]}

    def close_session(self, req: SessionCloseRequest) -> Dict[str, Any]:
        sid = make_session_id(req.project_id, req.session_title, req.session_summary)
        created_at = utc_now()
        session_fields = {
            "session_id": sid,
            "project_id": req.project_id,
            "session_title": req.session_title,
            "session_date": created_at,
            "session_raw_summary": req.session_summary,
            "session_capsule": f"Session: {req.session_title}. Summary: {req.session_summary[:600]}",
            "session_dense_line": f"{sid}|T=session|Title={req.session_title[:80]}|Sum={req.session_summary[:180]}",
            "decisions_json": json.dumps([i.model_dump() for i in req.decisions], ensure_ascii=False),
            "tasks_json": json.dumps([i.model_dump() for i in req.tasks], ensure_ascii=False),
            "issues_json": json.dumps([i.model_dump() for i in req.issues], ensure_ascii=False),
            "next_actions": "\n".join(req.next_actions),
            "source_chat_ref": req.source_chat_ref or "",
            "created_at": created_at,
        }
        self.client.create_records(TABLES["sessions"], [session_fields])

        created_memories = []
        memory_reqs: List[MemoryWriteRequest] = []
        mapping = [
            (RecordType.decision, req.decisions),
            (RecordType.task, req.tasks),
            (RecordType.issue, req.issues),
            (RecordType.architecture, req.architecture_notes),
            (RecordType.validation, req.validation_notes),
        ]
        for record_type, items in mapping:
            for item in items:
                memory_reqs.append(MemoryWriteRequest(
                    project_id=req.project_id,
                    record_type=record_type,
                    title=item.title,
                    raw_body=item.body or item.summary,
                    human_summary=item.summary,
                    priority=item.priority,
                    tags=[*item.tags, record_type.value],
                    source_refs=[sid] if sid else [],
                    metadata={"session_id": sid, "capture_mode": "session_close"},
                    review_status=req.review_status,
                ))
        # Always create a handover/session-summary memory record as well.
        memory_reqs.insert(0, MemoryWriteRequest(
            project_id=req.project_id,
            record_type=RecordType.handover_summary,
            title=req.session_title,
            raw_body=req.session_summary,
            human_summary=req.session_summary[:600],
            priority=Priority.high,
            tags=["session", "handover", "summary"],
            source_refs=[sid],
            metadata={"session_id": sid, "next_actions": req.next_actions},
            review_status=req.review_status,
        ))
        for m in memory_reqs:
            created_memories.append(self.write_memory(m))

        # Also write user-visible task and issue tables for dashboard views.
        self._write_task_rows(req, sid, created_at)
        self._write_issue_rows(req, sid, created_at)
        return {
            "session_id": sid,
            "created_memory_count": len(created_memories),
            "created_memory_ids": [m["memory_id"] for m in created_memories],
            "next_actions": req.next_actions,
        }

    def log_validation(self, req: ValidationLogRequest) -> Dict[str, Any]:
        vid = make_validation_id(req.project_id, req.test_case, req.actual_result)
        now = utc_now()
        fields = {
            "test_id": vid,
            "project_id": req.project_id,
            "system": req.system,
            "endpoint_or_feature": req.endpoint_or_feature,
            "test_case": req.test_case,
            "expected_result": req.expected_result,
            "actual_result": req.actual_result,
            "result": req.result,
            "evidence_ref": req.evidence_ref or "",
            "linked_issue_id": req.linked_issue_id or "",
            "created_at": now,
        }
        rec = self.client.create_records(TABLES["validation"], [fields])[0]
        # Mirror as a memory record.
        mem = self.write_memory(MemoryWriteRequest(
            project_id=req.project_id,
            record_type=RecordType.validation,
            title=f"Validation: {req.endpoint_or_feature}",
            raw_body=f"Test case: {req.test_case}\nExpected: {req.expected_result}\nActual: {req.actual_result}\nResult: {req.result}",
            human_summary=f"{req.endpoint_or_feature}: {req.result}",
            priority=Priority.high if req.result in {"fail", "blocked"} else Priority.medium,
            tags=["validation", req.system, req.result],
            source_refs=[vid] + ([req.evidence_ref] if req.evidence_ref else []),
        ))
        return {"test_id": vid, "airtable_record_id": rec.get("id"), "memory_id": mem["memory_id"]}

    def open_tasks(self, project_id: str) -> Dict[str, Any]:
        rows = self.client.list_records(
            TABLES["tasks"],
            formula=f"AND({project_formula(project_id)}, OR({{status}}='open', {{status}}='in_progress', {{status}}='blocked'))",
            max_records=50,
        )
        return {"project_id": project_id, "tasks": [r.get("fields", {}) for r in rows]}

    def open_issues(self, project_id: str) -> Dict[str, Any]:
        rows = self.client.list_records(
            TABLES["issues"],
            formula=f"AND({project_formula(project_id)}, OR({{status}}='open', {{status}}='investigating'))",
            max_records=50,
        )
        return {"project_id": project_id, "issues": [r.get("fields", {}) for r in rows]}

    def _rank_records(self, query: str, records: List[Dict[str, Any]], tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        q_terms = {t.lower() for t in query.replace("/", " ").replace("_", " ").split() if len(t) > 2}
        tag_terms = {t.lower() for t in (tags or [])}
        def score(rec: Dict[str, Any]) -> int:
            f = rec.get("fields", {})
            hay = " ".join(str(f.get(k, "")) for k in ["title", "human_summary", "semantic_capsule", "ai_dense_line", "retrieval_hint", "tags"]).lower()
            s = sum(3 for t in q_terms if t in hay) + sum(5 for t in tag_terms if t in hay)
            priority = str(f.get("priority", "")).lower()
            if priority == "critical":
                s += 3
            elif priority == "high":
                s += 2
            return s
        return sorted(records, key=score, reverse=True)

    def _write_task_rows(self, req: SessionCloseRequest, session_id: str, created_at: str) -> None:
        if not req.tasks:
            return
        rows = []
        for item in req.tasks:
            rows.append({
                "task_id": f"TASK-{session_id}-{len(rows)+1:02d}",
                "project_id": req.project_id,
                "title": item.title,
                "status": "open",
                "priority": item.priority.value,
                "owner": "",
                "due_date": "",
                "linked_memory_ids": session_id,
                "notes": item.summary,
                "created_at": created_at,
                "updated_at": created_at,
            })
        self.client.create_records(TABLES["tasks"], rows)

    def _write_issue_rows(self, req: SessionCloseRequest, session_id: str, created_at: str) -> None:
        if not req.issues:
            return
        rows = []
        for item in req.issues:
            rows.append({
                "issue_id": f"ISS-{session_id}-{len(rows)+1:02d}",
                "project_id": req.project_id,
                "title": item.title,
                "severity": item.priority.value,
                "status": "open",
                "symptom": item.summary,
                "root_cause": "",
                "workaround": "",
                "resolution": "",
                "linked_memory_ids": session_id,
                "created_at": created_at,
                "updated_at": created_at,
            })
        self.client.create_records(TABLES["issues"], rows)
