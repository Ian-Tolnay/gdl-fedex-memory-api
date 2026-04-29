from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from models import MemoryWriteRequest, RecordType

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "for", "with", "on", "at", "by", "from",
    "this", "that", "these", "those", "is", "are", "was", "were", "be", "been", "being", "as", "it", "we", "our",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug_words(text: str, limit: int = 8) -> List[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}", text.lower())
    result: List[str] = []
    for word in words:
        if word in STOPWORDS:
            continue
        if word not in result:
            result.append(word)
        if len(result) >= limit:
            break
    return result


def token_estimate(text: str) -> int:
    # Rough estimate. We intentionally avoid tokenizer dependencies in v0.1.
    return max(1, int(len(text) / 4))


def content_hash(project_id: str, title: str, body: str) -> str:
    raw = f"{project_id}\n{title}\n{body}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def make_memory_id(project_id: str, title: str, body: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"MEM-{today}-{content_hash(project_id, title, body)[:8]}"


def make_session_id(project_id: str, title: str, summary: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"SES-{today}-{content_hash(project_id, title, summary)[:8]}"


def make_validation_id(project_id: str, test_case: str, actual_result: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"VAL-{today}-{content_hash(project_id, test_case, actual_result)[:8]}"


def clean_tag(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\- ]", "", tag).strip().lower().replace(" ", "_")[:40]


def infer_tags(title: str, body: str, existing: Optional[Iterable[str]] = None) -> List[str]:
    tags = [clean_tag(t) for t in (existing or []) if clean_tag(t)]
    for word in slug_words(f"{title} {body}", limit=10):
        tag = clean_tag(word)
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:12]


def semantic_capsule(req: MemoryWriteRequest) -> str:
    summary = req.human_summary or first_sentence(req.raw_body, 260)
    tag_text = ",".join(infer_tags(req.title, req.raw_body, req.tags)[:8])
    return (
        f"Type: {req.record_type.value}. Title: {req.title}. "
        f"Summary: {summary}. Priority: {req.priority.value}. "
        f"Status: {req.status.value}. Tags: {tag_text}."
    )


def first_sentence(text: str, max_chars: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    match = re.search(r"(.{20,}?[.!?])\s", compact)
    if match and len(match.group(1)) <= max_chars:
        return match.group(1)
    return compact[:max_chars].rstrip() + ("..." if len(compact) > max_chars else "")


def dense_line(memory_id: str, req: MemoryWriteRequest, tags: List[str]) -> str:
    summary = (req.human_summary or first_sentence(req.raw_body, 140)).replace("|", "/")
    tag_text = "+".join(tags[:6])
    linked = "+".join(req.linked_record_ids[:5]) if req.linked_record_ids else "none"
    return (
        f"{memory_id}|T={req.record_type.value}|P={req.priority.value}|S={req.status.value}|"
        f"Title={req.title[:80].replace('|','/')}|Sum={summary}|Tags={tag_text}|Linked={linked}"
    )


def retrieval_hint(req: MemoryWriteRequest, tags: List[str]) -> str:
    base = slug_words(f"{req.title} {req.raw_body}", limit=18)
    merged = []
    for item in [*tags, *base, req.record_type.value]:
        clean = clean_tag(item)
        if clean and clean not in merged:
            merged.append(clean)
    return " ".join(merged[:30])


def simple_graph_edges(memory_id: str, req: MemoryWriteRequest) -> List[Dict[str, Any]]:
    """Heuristic graph extraction for v0.1. Replace with Structured Outputs later."""
    text = f"{req.title}. {req.raw_body}"
    edges: List[Dict[str, Any]] = []
    patterns = [
        (r"([A-Z][A-Za-z0-9_ ]{2,40})\s+(?:calls|triggers|uses|requires|depends on|precedes)\s+([A-Z][A-Za-z0-9_ ]{2,40})", "related_to"),
        (r"([A-Za-z0-9_\- ]{3,40})\s*->\s*([A-Za-z0-9_\- ]{3,40})", "flows_to"),
        (r"([A-Za-z0-9_\- ]{3,40})\s*→\s*([A-Za-z0-9_\- ]{3,40})", "flows_to"),
    ]
    for pattern, relationship in patterns:
        for match in re.finditer(pattern, text):
            source = match.group(1).strip()[:80]
            target = match.group(2).strip()[:80]
            if source.lower() != target.lower():
                edges.append({
                    "edge_id": f"EDGE-{content_hash(memory_id, source, target)[:10]}",
                    "source_entity": source,
                    "relationship": relationship,
                    "target_entity": target,
                    "source_memory_id": memory_id,
                    "confidence": 0.45,
                    "status": "draft",
                })
    return edges[:8]


def compile_memory(req: MemoryWriteRequest) -> Dict[str, Any]:
    mid = make_memory_id(req.project_id, req.title, req.raw_body)
    tags = infer_tags(req.title, req.raw_body, req.tags)
    capsule = semantic_capsule(req)
    dense = dense_line(mid, req, tags)
    raw_tokens = token_estimate(req.raw_body)
    dense_tokens = token_estimate(dense)
    created = utc_now()
    return {
        "memory_id": mid,
        "project_id": req.project_id,
        "record_type": req.record_type.value,
        "title": req.title,
        "raw_body": req.raw_body,
        "human_summary": req.human_summary or first_sentence(req.raw_body),
        "semantic_capsule": capsule,
        "ai_dense_line": dense,
        "retrieval_hint": retrieval_hint(req, tags),
        "status": req.status.value,
        "priority": req.priority.value,
        "tags": tags,
        "linked_record_ids": req.linked_record_ids,
        "source_refs": req.source_refs,
        "confidence": req.confidence,
        "review_status": req.review_status.value,
        "metadata_json": json.dumps(req.metadata, ensure_ascii=False),
        "entity_triples_json": json.dumps(simple_graph_edges(mid, req), ensure_ascii=False),
        "claims_json": json.dumps([], ensure_ascii=False),
        "causal_links_json": json.dumps([], ensure_ascii=False),
        "token_estimate_raw": raw_tokens,
        "token_estimate_dense": dense_tokens,
        "compression_ratio": round(raw_tokens / max(dense_tokens, 1), 2),
        "content_hash": content_hash(req.project_id, req.title, req.raw_body),
        "created_at": created,
        "updated_at": created,
    }
