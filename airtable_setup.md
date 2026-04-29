# Airtable Setup — GDL FedEx Engineer Memory

Create a new Airtable base named:

```text
GDL FedEx Engineer Memory
```

Use the field names exactly as written below. For v0.1, most fields can be `single line text` or `long text`. Once the system is working, you can improve field types with single-selects, linked records, and views.

## Projects

Primary field: `project_id`

Fields:

```text
project_id
project_name
status
current_phase
objective
bootstrap_summary
active_rules
last_session_summary
updated_at
```

Initial record:

```text
project_id: gdl-fedex-mk2
project_name: FedEx In-House Engineer MK2
status: active
current_phase: Memory/API architecture setup
objective: Build durable project memory for the GDL shipping/FedEx connector project.
```

## Memory_Records

Primary field: `memory_id`

Fields:

```text
memory_id
project_id
record_type
title
raw_body
human_summary
semantic_capsule
ai_dense_line
retrieval_hint
status
priority
tags
linked_record_ids
source_refs
confidence
review_status
metadata_json
entity_triples_json
claims_json
causal_links_json
token_estimate_raw
token_estimate_dense
compression_ratio
content_hash
created_at
updated_at
```

## Sessions

Primary field: `session_id`

Fields:

```text
session_id
project_id
session_title
session_date
session_raw_summary
session_capsule
session_dense_line
decisions_json
tasks_json
issues_json
changed_assumptions_json
do_not_repeat_json
next_actions
source_chat_ref
created_at
```

## Tasks

Primary field: `task_id`

Fields:

```text
task_id
project_id
title
status
priority
owner
due_date
linked_memory_ids
notes
created_at
updated_at
```

## Issues

Primary field: `issue_id`

Fields:

```text
issue_id
project_id
title
severity
status
symptom
root_cause
workaround
resolution
linked_memory_ids
created_at
updated_at
```

## Sources

Primary field: `source_id`

Fields:

```text
source_id
project_id
title
source_type
version
source_ref
summary
vector_file_id
vector_store_id
uploaded_to_gpt_knowledge
created_at
```

## Validation_Results

Primary field: `test_id`

Fields:

```text
test_id
project_id
system
endpoint_or_feature
test_case
expected_result
actual_result
result
evidence_ref
linked_issue_id
created_at
```

## Shipping_Rules

Primary field: `rule_id`

Fields:

```text
rule_id
project_id
rule_type
title
condition
rule
status
priority
source_ref
linked_memory_ids
created_at
updated_at
```

## Graph_Edges

Primary field: `edge_id`

Fields:

```text
edge_id
project_id
source_entity
relationship
target_entity
source_memory_id
confidence
status
created_at
```

## Recommended views

- Memory_Records: Recent Active Memory
- Memory_Records: Pending Review
- Memory_Records: Decisions
- Memory_Records: Architecture
- Tasks: Open Tasks
- Issues: Open Issues
- Validation_Results: Failed or Blocked Tests
- Shipping_Rules: Active Shipping Rules
- Graph_Edges: Draft Graph Edges
