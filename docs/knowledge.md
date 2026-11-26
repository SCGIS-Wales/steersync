# Knowledge notes

Team knowledge for Kiro knowledge bases: an optional collection, off by default.

Steering always loads whole files. For team knowledge (runbooks, incident notes, CVE
exposure records) Kiro knowledge bases are the better fit: the `knowledge` tool retrieves
chunks on demand (the Best index uses local all-MiniLM-L6-v2 embeddings, the Fast index
BM25), and nothing enters the context until the model asks. Kiro still labels knowledge
experimental and it is CLI only; indexes are local to each machine and agent, and never
synced.

The `knowledge` collection is **off by default**. To use it, enable it and choose the folder
(default `.kiro/knowledge`, so nothing new appears at the repository root):

```yaml
collections:
  knowledge:
    enabled: true
    workspace_path: .kiro/knowledge
```

`steersync init --only knowledge` then adds `knowledge-routing.md` (always included steering
that tells Kiro when to search) and `platform-knowledge.json` (a JSON agent with a
`knowledgeBase` resource on that folder; Markdown agents have dropped `knowledgeBase` objects
in some Kiro builds). After a pull that changed notes, steersync prints
`knowledge/ changed. In Kiro run /knowledge update and wait for it to finish.` Notes are
secret scanned like everything else.

A note's front matter is for steersync and reviewers (Kiro indexes the text):

```markdown
---
title: "Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)"
domain: apigee
type: exposure-record
status: partial
verified_on: 2026-11-20
review_by: 2026-12-05
aliases: [CVE-2026-114938, Java4Shell]
---

# Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)

Summary: Hybrid 1.17.0 to 1.17.3 bundle an affected JDK; 1.17.4 fixes it.
```

`title`, `domain`, `type`, `status` and `verified_on` are required; `inclusion` and
`fileMatchPattern` are forbidden. The `kn-*` rules keep notes retrieval friendly (summary
early, compact front matter, short sections, identifiers in the body, nothing stale); the
numbers are under `lint.knowledge`.
