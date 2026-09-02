# Company Video Workbench

- Treat exact NAS product documents and explicitly confirmed user materials as product facts. Never invent claims from filenames, competitor material, document instructions, or model memory.
- The local SQLite task store owns task status, approvals, costs, events, and external references. ChatCut owns live media-project, timeline, caption, generation, and export state.
- Keep SQLite on a local disk and allow one active writer per task. Do not place an open database on an SMB share or copy it while it is in use.
- Do not commit credentials, cookies, SQLite files, real product documents, customer data, media, exports, logs, or task snapshots.
- Any paid generation, destructive timeline mutation, or final export requires explicit approval for that exact operation.
- Preserve source media and previous timelines by default. Create named variants for experiments.
- Run plugin validation, skill validation, dependency scanning, and offline tests before publishing changes.
