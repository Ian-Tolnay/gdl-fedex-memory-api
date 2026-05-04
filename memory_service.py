from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from airtable_client import AirtableClient, AirtableError, airtable_formula_equals
from memory_compiler import compile_memory, make_session_id, make_validation_id, utc_now
from models import (
    BrainCommandRequest,
    MemorySearchRequest,
    MemoryScope,
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

def scope_formula(scope: MemoryScope) -> Optional[str]:
    if scope == MemoryScope.active_only:
        return "{status}='active'"

    if scope == MemoryScope.pending_review:
        return "OR({review_status}='pending_review', {status}='pending_review')"

    if scope == MemoryScope.active_and_pending:
        return "OR({status}='active', {status}='pending_review')"

    if scope == MemoryScope.all_non_deprecated:
        return "NOT({status}='deprecated')"

    if scope == MemoryScope.all:
        return None

    return "NOT({status}='deprecated')"

class MemoryService:
    def __init__(self, client: Optional[AirtableClient] = None):
        self.client = client or AirtableClient()

    def project_bootstrap(self, project_id: str, scope: MemoryScope = MemoryScope.all_non_deprecated) -> Dict[str, Any]:
        project_records = self.client.list_records(TABLES["projects"], formula=project_formula(project_id), max_records=1)
        project = project_records[0].get("fields", {}) if project_records else {}

        recent_formulas = [project_formula(project_id)]
        scope_filter = scope_formula(scope)
        if scope_filter:
            recent_formulas.append(scope_filter)

        recent_formula = "AND(" + ", ".join(recent_formulas) + ")" if len(recent_formulas) > 1 else recent_formulas[0]

        recent = self.client.list_records(
            TABLES["memory"],
            formula=recent_formula,
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
        else:
            scope_filter = scope_formula(req.scope)
            if scope_filter:
                formulas.append(scope_filter)

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

        update_fields = {


            "status": new_status.value,


            "review_status": "reviewed",


            "metadata_json": metadata_json,


            "updated_at": utc_now(),


        }


        update_fields.update(self._lifecycle_text_updates(fields, new_status.value))



        updated = self.client.update_record(TABLES["memory"], rec["id"], update_fields)

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

        update_fields = {


            "status": "deprecated",


            "review_status": "rejected",


            "metadata_json": metadata_json,


            "updated_at": utc_now(),


        }


        update_fields.update(self._lifecycle_text_updates(fields, "deprecated"))



        updated = self.client.update_record(TABLES["memory"], rec["id"], update_fields)


        mirrored_sync = self._sync_mirrored_record_status(memory_id, fields, action="reject")

        return {
            "memory_id": memory_id,
            "airtable_record_id": updated.get("id"),
            "status": "deprecated",
            "review_status": "rejected",
            "message": "Memory rejected",
            "mirrored_sync": mirrored_sync,
        }

    def _lifecycle_text_updates(self, fields: Dict[str, Any], status_value: str) -> Dict[str, Any]:
        """
        Refresh derived GPT-facing text fields after status/review changes.

        Earlier v0.1 records compiled semantic_capsule and ai_dense_line once at creation time.
        If a memory was later approved/rejected, the real Airtable status changed but these
        dense text fields could still say S=pending_review. This keeps retrieved context honest.
        """
        updates: Dict[str, Any] = {}

        capsule = str(fields.get("semantic_capsule") or "")
        if capsule:
            if re.search(r"Status:\s*[^.]+", capsule):
                capsule = re.sub(r"Status:\s*[^.]+", f"Status: {status_value}", capsule)
            else:
                capsule = capsule.rstrip(".") + f". Status: {status_value}."
            updates["semantic_capsule"] = capsule

        dense = str(fields.get("ai_dense_line") or "")
        if dense:
            if "|S=" in dense:
                dense = re.sub(r"\|S=[^|]*", f"|S={status_value}", dense)
            else:
                dense = dense + f"|S={status_value}"
            updates["ai_dense_line"] = dense

        return updates

    def _find_mirrored_records(self, table: str, memory_id: str) -> List[Dict[str, Any]]:
        """Find task/issue dashboard rows linked to a memory_id."""
        formula = f"FIND('{memory_id}', {{linked_memory_ids}}) > 0"
        try:
            return self.client.list_records(table, formula=formula, max_records=25)
        except Exception:
            return []

    def _sync_mirrored_record_status(self, memory_id: str, fields: Dict[str, Any], action: str) -> List[Dict[str, Any]]:
        """
        Keep mirrored dashboard rows aligned with rejected memory.

        If a task/issue memory is rejected as test/noise, its mirrored Tasks/Issues row should
        stop appearing as a live open item in bootstrap/open-task/open-issue views.
        """
        synced: List[Dict[str, Any]] = []
        record_type = str(fields.get("record_type") or "")
        now = utc_now()

        if action != "reject":
            return synced

        if record_type == "task":
            for rec in self._find_mirrored_records(TABLES["tasks"], memory_id):
                existing = rec.get("fields", {})
                existing_notes = str(existing.get("notes") or "")
                note = (existing_notes + "\n[Memory review] Cancelled because linked memory was rejected.").strip()
                self.client.update_record(TABLES["tasks"], rec["id"], {
                    "status": "cancelled",
                    "notes": note,
                    "updated_at": now,
                })
                synced.append({"table": "Tasks", "airtable_record_id": rec.get("id"), "status": "cancelled"})

        elif record_type == "issue":
            for rec in self._find_mirrored_records(TABLES["issues"], memory_id):
                existing = rec.get("fields", {})
                existing_resolution = str(existing.get("resolution") or "")
                resolution = (existing_resolution + "\n[Memory review] Parked because linked memory was rejected.").strip()
                self.client.update_record(TABLES["issues"], rec["id"], {
                    "status": "parked",
                    "resolution": resolution,
                    "updated_at": now,
                })
                synced.append({"table": "Issues", "airtable_record_id": rec.get("id"), "status": "parked"})

        return synced

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

    def update_task_status(self, task_id: str, status: str, note: Optional[str] = None) -> Dict[str, Any]:
        records = self.client.list_records(
            TABLES["tasks"],
            formula=airtable_formula_equals("task_id", task_id),
            max_records=1,
        )
        if not records:
            raise AirtableError(f"No task found for task_id={task_id}")

        rec = records[0]
        fields = rec.get("fields", {})
        updates: Dict[str, Any] = {
            "status": status,
            "updated_at": utc_now(),
        }

        if note:
            existing = str(fields.get("notes") or "")
            updates["notes"] = (existing + f"\n[Status update] {note}").strip()

        updated = self.client.update_record(TABLES["tasks"], rec["id"], updates)
        return {
            "task_id": task_id,
            "airtable_record_id": updated.get("id"),
            "status": status,
            "message": "Task status updated",
        }

    def bulk_update_task_status(self, task_ids: List[str], status: str, note: Optional[str] = None) -> Dict[str, Any]:
        updated: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for task_id in task_ids:
            try:
                updated.append(self.update_task_status(task_id, status, note))
            except Exception as exc:
                errors.append({"task_id": task_id, "error": str(exc)})

        return {
            "status": status,
            "requested_count": len(task_ids),
            "updated_count": len(updated),
            "error_count": len(errors),
            "updated": updated,
            "errors": errors,
        }

    def update_issue_status(
        self,
        issue_id: str,
        status: str,
        resolution: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        records = self.client.list_records(
            TABLES["issues"],
            formula=airtable_formula_equals("issue_id", issue_id),
            max_records=1,
        )
        if not records:
            raise AirtableError(f"No issue found for issue_id={issue_id}")

        rec = records[0]
        fields = rec.get("fields", {})
        updates: Dict[str, Any] = {
            "status": status,
            "updated_at": utc_now(),
        }

        if resolution:
            existing_resolution = str(fields.get("resolution") or "")
            updates["resolution"] = (existing_resolution + f"\n{resolution}").strip()

        if note:
            existing_resolution = str(updates.get("resolution") or fields.get("resolution") or "")
            updates["resolution"] = (existing_resolution + f"\n[Status note] {note}").strip()

        updated = self.client.update_record(TABLES["issues"], rec["id"], updates)
        return {
            "issue_id": issue_id,
            "airtable_record_id": updated.get("id"),
            "status": status,
            "message": "Issue status updated",
        }

    def bulk_update_issue_status(
        self,
        issue_ids: List[str],
        status: str,
        resolution: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        updated: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for issue_id in issue_ids:
            try:
                updated.append(self.update_issue_status(issue_id, status, resolution, note))
            except Exception as exc:
                errors.append({"issue_id": issue_id, "error": str(exc)})

        return {
            "status": status,
            "requested_count": len(issue_ids),
            "updated_count": len(updated),
            "error_count": len(errors),
            "updated": updated,
            "errors": errors,
        }

    def brain_command(self, req: BrainCommandRequest) -> Dict[str, Any]:
        """
        Natural-language Brain command router.

        v0.2.0 is deterministic and conservative:
        - Routes common Brain commands to existing memory endpoints.
        - Keeps GPT instructions small.
        - Auto-updates task/issue status only when session text clearly matches open items.
        """
        command = (req.command_text or "").strip()
        intent = self._classify_brain_intent(command)

        response: Dict[str, Any] = {
            "project_id": req.project_id,
            "intent": intent,
            "command_text": command,
            "router_version": "v0.2.3-deterministic",
            "actions": [],
            "warnings": [],
        }

        if intent == "wake":
            result = self.project_bootstrap(req.project_id, req.scope)
            response["actions"].append({"action": "project_bootstrap", "scope": req.scope.value})
            response["result"] = result
            return response

        if intent == "search":
            query = self._extract_brain_query(command)
            result = self.build_context(
                req.project_id,
                query,
                req.token_budget,
                record_types=[],
                include_raw=req.include_raw,
                scope=req.scope,
            )
            response["actions"].append({"action": "build_context", "query": query, "scope": req.scope.value})
            response["result"] = result
            return response

        if intent == "pending_review":
            result = self.pending_reviews(req.project_id, req.limit)
            response["actions"].append({"action": "pending_reviews", "limit": req.limit})
            response["result"] = result
            return response

        if intent == "open_tasks":
            result = self.open_tasks(req.project_id)
            response["actions"].append({"action": "open_tasks"})
            response["result"] = result
            return response

        if intent == "open_issues":
            result = self.open_issues(req.project_id)
            response["actions"].append({"action": "open_issues"})
            response["result"] = result
            return response

        if intent == "task_status":
            result = self._brain_update_task_from_command(req.project_id, command)
            response["actions"].append({"action": "task_status_from_command", "result": result})
            response["result"] = result
            return response

        if intent == "issue_status":
            result = self._brain_update_issue_from_command(req.project_id, command)
            response["actions"].append({"action": "issue_status_from_command", "result": result})
            response["result"] = result
            return response

        if intent == "commit":
            summary = (req.visible_context_summary or "").strip()
            if not summary:
                summary = command

            session_title = self._make_brain_session_title(command, summary)
            sections = self._parse_brain_commit_sections(summary)

            session_req = SessionCloseRequest(
                project_id=req.project_id,
                session_title=session_title,
                session_summary=sections.get("session_summary") or summary,
                decisions=sections.get("decisions", []),
                tasks=sections.get("tasks", []),
                issues=sections.get("issues", []),
                architecture_notes=sections.get("architecture_notes", []),
                validation_notes=sections.get("validation_notes", []),
                next_actions=sections.get("next_actions", []),
                source_chat_ref=req.source_chat_ref,
                review_status=req.review_status,
            )
            if req.auto_status_updates:
                auto_updates = self._auto_status_from_text(req.project_id, summary)
                response["actions"].append({"action": "auto_status_updates_pre_commit", "result": auto_updates})
                response["auto_status_updates"] = auto_updates

            result = self.close_session(session_req)
            response["actions"].append({"action": "close_session_to_memory", "session_title": session_title})
            response["result"] = result

            return response

        # Default: quick capture.
        capture_type = self._infer_brain_capture_type(command)
        text = (req.visible_context_summary or command).strip()
        title = self._make_brain_title(command, capture_type.value)

        quick_req = QuickCaptureRequest(
            project_id=req.project_id,
            capture_type=capture_type,
            text=text,
            title=title,
            priority=Priority.high if capture_type in {RecordType.issue, RecordType.decision, RecordType.architecture} else Priority.medium,
            tags=["brain_command", capture_type.value],
            source_ref=req.source_chat_ref,
            review_status=req.review_status,
        )

        if hasattr(self, "capture_quick"):
            result = self.capture_quick(quick_req)
        else:
            result = self.write_memory(MemoryWriteRequest(
                project_id=req.project_id,
                record_type=capture_type,
                title=title,
                raw_body=text,
                human_summary=text[:500],
                priority=quick_req.priority,
                tags=quick_req.tags,
                source_refs=[req.source_chat_ref] if req.source_chat_ref else [],
                review_status=req.review_status,
                metadata={"capture_mode": "brain_command"},
            ))

        response["actions"].append({"action": "quick_capture", "capture_type": capture_type.value})
        response["result"] = result
        return response

    def _classify_brain_intent(self, command: str) -> str:
        c = command.lower().strip()
        if any(p in c for p in [
            "brain save",
            "save this",
            "remember this",
            "log this",
            "add this to brain",
            "brain note",
        ]):
            return "quick_capture"


        if any(p in c for p in [
            "brain done",
            "task done",
            "complete task",
            "mark task",
            "mark the task",
            "task is done",
            "task is complete",
            "cancel task",
            "block task",
            "set task",
            "move task",
        ]):
            return "task_status"

        if any(p in c for p in [
            "brain resolved",
            "issue resolved",
            "resolve issue",
            "mark issue",
            "park issue",
            "issue is resolved",
            "issue is fixed",
            "set issue",
            "move issue",
            "reopen issue",
        ]):
            return "issue_status"


        if any(p in c for p in [
            "wake brain",
            "load project memory",
            "bring yourself up to date",
            "bring yourself up-to-date",
            "check system memory and bring yourself up to date",
            "latest project goals",
        ]):
            return "wake"

        if any(p in c for p in [
            "brain review",
            "pending memory",
            "pending brain",
            "brain inbox",
            "awaiting review",
            "memory reviews",
        ]):
            return "pending_review"

        if any(p in c for p in [
            "open tasks",
            "active tasks",
            "current tasks",
            "what tasks",
            "tasks we are working on",
        ]):
            return "open_tasks"

        if any(p in c for p in [
            "open issues",
            "active issues",
            "current issues",
            "what issues",
            "issues we are working on",
        ]):
            return "open_issues"

        if any(p in c for p in [
            "brain commit",
            "upload all new memory",
            "log all the things",
            "log everything",
            "close this session",
            "commit this session",
            "save today's progress",
            "save today’s progress",
            "since the last memory upload",
            "since last memory upload",
            "since the last brain",
        ]):
            return "commit"

        if any(p in c for p in [
            "search brain",
            "search system memory",
            "check brain for",
            "what does brain remember",
            "search memory",
            "build context",
        ]):
            return "search"

        return "quick_capture"

    def _extract_brain_query(self, command: str) -> str:
        c = command.strip()

        patterns = [
            r"search\s+(?:brain|system memory|memory)(?:\s+for)?\s*(.*)",
            r"check\s+brain\s+for\s+(.*)",
            r"what\s+does\s+brain\s+remember\s+(?:about)?\s*(.*)",
            r"build\s+(?:a\s+)?context(?:\s+pack)?(?:\s+for)?\s*(.*)",
        ]

        for pattern in patterns:
            m = re.search(pattern, c, flags=re.IGNORECASE)
            if m and m.group(1).strip():
                return m.group(1).strip(" .?")

        return c

    def _infer_brain_capture_type(self, command: str) -> RecordType:
        c = command.lower()

        if any(w in c for w in ["issue", "bug", "problem", "error", "blocked", "blocker", "failure", "failed"]):
            return RecordType.issue

        if any(w in c for w in ["task", "todo", "to-do", "next action", "action item", "need to"]):
            return RecordType.task

        if any(w in c for w in ["decision", "decided", "choose", "chosen", "confirmed direction"]):
            return RecordType.decision

        if any(w in c for w in ["architecture", "endpoint", "schema", "data model", "system design", "flow"]):
            return RecordType.architecture

        if any(w in c for w in ["validation", "test passed", "test failed", "verified", "smoke test"]):
            return RecordType.validation

        return RecordType.note

    def _make_brain_title(self, command: str, fallback_type: str) -> str:
        cleaned = re.sub(r"^(brain\s+save|save|remember|log|add\s+this)\s*[:\-]?\s*", "", command.strip(), flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if not cleaned:
            return f"Brain {fallback_type} capture"
        return cleaned[:180]

    def _make_brain_session_title(self, command: str, summary: str) -> str:
        if "session title:" in summary.lower():
            for line in summary.splitlines():
                if line.lower().startswith("session title:"):
                    title = line.split(":", 1)[1].strip()
                    if title:
                        return title[:180]

        base = command.strip()
        if len(base) > 20 and not base.lower().startswith("brain commit"):
            return base[:180]

        first_sentence = re.split(r"[.\n]", summary.strip())[0].strip()
        return (first_sentence or "Brain session commit")[:180]

    def _parse_brain_commit_sections(self, text: str) -> Dict[str, Any]:
        text = text.replace("\\r\\n", chr(10)).replace("\\n", chr(10))

        # Parse a GPT-provided visible_context_summary into structured session sections.
        buckets: Dict[str, List[str]] = {
            "summary": [],
            "decisions": [],
            "tasks": [],
            "issues": [],
            "architecture_notes": [],
            "validation_notes": [],
            "next_actions": [],
        }

        aliases = {
            "summary": "summary",
            "session summary": "summary",
            "decisions": "decisions",
            "decision": "decisions",
            "tasks": "tasks",
            "task": "tasks",
            "issues": "issues",
            "issue": "issues",
            "architecture": "architecture_notes",
            "architecture notes": "architecture_notes",
            "architecture note": "architecture_notes",
            "validation": "validation_notes",
            "validation notes": "validation_notes",
            "validation note": "validation_notes",
            "next actions": "next_actions",
            "next action": "next_actions",
            "next steps": "next_actions",
        }

        current = "summary"

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            heading = re.match(r"^([A-Za-z _/-]+)\s*:\s*(.*)$", line)
            if heading:
                key = heading.group(1).strip().lower().replace("-", " ")
                rest = heading.group(2).strip()
                if key in aliases:
                    current = aliases[key]
                    if rest:
                        buckets[current].append(rest)
                    continue

            cleaned = re.sub(r"^\s*[-*•]?\s*\d*[\).]?\s*", "", line).strip()
            if cleaned:
                buckets[current].append(cleaned)

        def priority_for(kind: str, value: str) -> Priority:
            lower = value.lower()
            if any(w in lower for w in ["critical", "security", "broken", "failed", "failure", "blocked"]):
                return Priority.critical
            if kind in {"decisions", "issues", "architecture_notes"}:
                return Priority.high
            return Priority.medium

        def make_items(kind: str) -> List[SessionItem]:
            items: List[SessionItem] = []
            for value in buckets.get(kind, []):
                if not value:
                    continue
                title = value[:120]
                items.append(SessionItem(
                    title=title,
                    summary=value,
                    body=value,
                    priority=priority_for(kind, value),
                    tags=["brain_commit", kind],
                ))
            return items

        session_summary = " ".join(buckets.get("summary") or []).strip()
        if not session_summary:
            session_summary = text.strip()[:1200]

        return {
            "session_summary": session_summary,
            "decisions": make_items("decisions"),
            "tasks": make_items("tasks"),
            "issues": make_items("issues"),
            "architecture_notes": make_items("architecture_notes"),
            "validation_notes": make_items("validation_notes"),
            "next_actions": buckets.get("next_actions", []),
        }

    def _extract_task_ids_from_command(self, command: str) -> List[str]:
        return list(dict.fromkeys(re.findall(r"TASK-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", command)))

    def _extract_issue_ids_from_command(self, command: str) -> List[str]:
        return list(dict.fromkeys(re.findall(r"ISS-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", command)))

    def _infer_task_status_from_command(self, command: str) -> str:
        c = command.lower()
        if any(w in c for w in ["cancel", "cancelled", "canceled", "no longer needed", "not needed"]):
            return "cancelled"
        if any(w in c for w in ["block", "blocked", "waiting", "stuck"]):
            return "blocked"
        if any(w in c for w in ["in progress", "started", "working on"]):
            return "in_progress"
        return "complete"

    def _infer_issue_status_from_command(self, command: str) -> str:
        c = command.lower()
        if any(w in c for w in ["park", "parked", "defer", "deferred"]):
            return "parked"
        if any(w in c for w in ["investigating", "investigate", "still checking"]):
            return "investigating"
        if any(w in c for w in ["reopen", "still open", "not resolved"]):
            return "open"
        return "resolved"

    def _brain_update_task_from_command(self, project_id: str, command: str) -> Dict[str, Any]:
        status = self._infer_task_status_from_command(command)
        task_ids = self._extract_task_ids_from_command(command)

        if task_ids:
            if len(task_ids) == 1:
                return self.update_task_status(task_ids[0], status, f"Updated by Brain command: {command}")
            return self.bulk_update_task_status(task_ids, status, f"Updated by Brain command: {command}")

        tasks = self.open_tasks(project_id).get("tasks", [])
        scored = []
        for task in tasks:
            title = str(task.get("title") or "")
            notes = str(task.get("notes") or "")
            task_id = str(task.get("task_id") or "")
            score = self._overlap_score(f"{title} {notes}", command)
            if task_id and score >= 0.45:
                scored.append((score, task))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return {
                "needs_disambiguation": True,
                "reason": "No strong task match found.",
                "intended_status": status,
                "open_tasks": tasks[:8],
            }

        top_score, top_task = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if top_score >= 0.65 or (top_score >= 0.5 and top_score - second_score >= 0.2):
            task_id = str(top_task.get("task_id"))
            update = self.update_task_status(task_id, status, f"Updated by Brain command: {command}")
            return {
                "matched_task": top_task,
                "match_score": round(top_score, 3),
                "update": update,
            }

        return {
            "needs_disambiguation": True,
            "reason": "Multiple possible task matches.",
            "intended_status": status,
            "candidates": [item for _, item in scored[:5]],
        }

    def _brain_update_issue_from_command(self, project_id: str, command: str) -> Dict[str, Any]:
        status = self._infer_issue_status_from_command(command)
        issue_ids = self._extract_issue_ids_from_command(command)

        if issue_ids:
            if len(issue_ids) == 1:
                return self.update_issue_status(
                    issue_ids[0],
                    status,
                    f"Updated by Brain command: {command}",
                    "Updated by Brain command.",
                )
            return self.bulk_update_issue_status(
                issue_ids,
                status,
                f"Updated by Brain command: {command}",
                "Updated by Brain command.",
            )

        issues = self.open_issues(project_id).get("issues", [])
        scored = []
        for issue in issues:
            title = str(issue.get("title") or "")
            symptom = str(issue.get("symptom") or "")
            issue_id = str(issue.get("issue_id") or "")
            score = self._overlap_score(f"{title} {symptom}", command)
            if issue_id and score >= 0.45:
                scored.append((score, issue))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return {
                "needs_disambiguation": True,
                "reason": "No strong issue match found.",
                "intended_status": status,
                "open_issues": issues[:8],
            }

        top_score, top_issue = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if top_score >= 0.65 or (top_score >= 0.5 and top_score - second_score >= 0.2):
            issue_id = str(top_issue.get("issue_id"))
            update = self.update_issue_status(
                issue_id,
                status,
                f"Updated by Brain command: {command}",
                "Updated by Brain command.",
            )
            return {
                "matched_issue": top_issue,
                "match_score": round(top_score, 3),
                "update": update,
            }

        return {
            "needs_disambiguation": True,
            "reason": "Multiple possible issue matches.",
            "intended_status": status,
            "candidates": [item for _, item in scored[:5]],
        }

    def _tokenize_for_match(self, text: str) -> set:
        stop = {
            "that", "this", "with", "from", "into", "have", "were", "been", "being",
            "task", "issue", "memory", "records", "record", "table", "quick", "capture",
            "confirmed", "confirm", "test", "testing", "working", "project",
        }
        words = re.findall(r"[a-zA-Z0-9_]{4,}", text.lower())
        return {w for w in words if w not in stop}

    def _overlap_score(self, title: str, text: str) -> float:
        title_terms = self._tokenize_for_match(title)
        if not title_terms:
            return 0.0
        text_terms = self._tokenize_for_match(text)
        overlap = len(title_terms & text_terms)
        return overlap / max(1, len(title_terms))

    def _auto_status_from_text(self, project_id: str, text: str) -> Dict[str, Any]:
        """
        Conservative operational cleanup.

        Only marks open tasks/issues closed when the session text contains strong completion/resolution cues
        and the title overlap is high enough.
        """
        lower = text.lower()

        completion_cues = [
            "completed",
            "done",
            "finished",
            "verified",
            "confirmed",
            "passed",
            "working as intended",
            "working correctly",
            "successfully tested",
            "tests passed",
        ]

        resolution_cues = [
            "resolved",
            "fixed",
            "no longer an issue",
            "working as intended",
            "working correctly",
            "confirmed fixed",
            "issue resolved",
            "tests passed",
        ]

        negative_cues = [
            "not complete",
            "not completed",
            "not done",
            "not resolved",
            "still failing",
            "still broken",
        ]

        if any(n in lower for n in negative_cues):
            return {"skipped": "Negative/incomplete cue detected; no automatic status updates applied."}

        updated_tasks: List[Dict[str, Any]] = []
        updated_issues: List[Dict[str, Any]] = []

        if any(cue in lower for cue in completion_cues) and hasattr(self, "update_task_status"):
            try:
                tasks = self.open_tasks(project_id).get("tasks", [])
                for task in tasks:
                    title = str(task.get("title") or "")
                    task_id = str(task.get("task_id") or "")
                    if task_id and self._overlap_score(title, text) >= 0.55:
                        updated_tasks.append(self.update_task_status(
                            task_id,
                            "complete",
                            "Auto-completed by Brain command router from session commit evidence.",
                        ))
            except Exception as exc:
                updated_tasks.append({"error": str(exc)})

        if any(cue in lower for cue in resolution_cues) and hasattr(self, "update_issue_status"):
            try:
                issues = self.open_issues(project_id).get("issues", [])
                for issue in issues:
                    title = str(issue.get("title") or "")
                    issue_id = str(issue.get("issue_id") or "")
                    if issue_id and self._overlap_score(title, text) >= 0.55:
                        updated_issues.append(self.update_issue_status(
                            issue_id,
                            "resolved",
                            "Auto-resolved by Brain command router from session commit evidence.",
                            "Conservative title-overlap match from Brain commit.",
                        ))
            except Exception as exc:
                updated_issues.append({"error": str(exc)})

        return {
            "updated_tasks": updated_tasks,
            "updated_issues": updated_issues,
            "task_count": len([x for x in updated_tasks if "error" not in x]),
            "issue_count": len([x for x in updated_issues if "error" not in x]),
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

    def build_context(
        self,
        project_id: str,
        query: str,
        token_budget: int,
        record_types: Optional[List[RecordType]] = None,
        include_raw: bool = False,
        scope: MemoryScope = MemoryScope.all_non_deprecated,
    ) -> Dict[str, Any]:
        search = self.search_memory(MemorySearchRequest(
            project_id=project_id,
            query=query,
            record_types=record_types or [],
            limit=25,
            include_raw=include_raw,
            scope=scope,
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
