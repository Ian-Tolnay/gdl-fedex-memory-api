# Memory Compiler Contract v0.1

Every captured project memory should eventually produce the following fields:

1. `raw_body` — full human-readable evidence or note
2. `human_summary` — concise readable summary
3. `semantic_capsule` — compact structured meaning
4. `ai_dense_line` — dense GPT-readable one-line memory
5. `retrieval_hint` — keywords and phrases for search
6. `record_type` — decision, task, issue, architecture, etc.
7. `priority` — low, medium, high, critical
8. `status` — draft, pending_review, active, superseded, deprecated
9. `tags` — compact topical labels
10. `linked_record_ids` — related memory/session/source IDs
11. `entity_triples_json` — graph-like relationships
12. `source_refs` — session/file/chat/source references
13. `confidence` — extraction confidence from 0 to 1
14. `review_status` — pending_review, auto_accepted, reviewed, rejected

## v0.1 behavior

This starter implementation creates these fields mostly with deterministic rules, not expensive LLM calls. That keeps first tests fast and cheap.

## v0.2 upgrade

Add an OpenAI Structured Outputs extraction step to produce better capsules, graph edges, claims, causal links, and supersession detection.
