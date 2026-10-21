# AgentGraph Content Rules

- Use explicit verbs: **Apply proposal**, **Reject**, **Approve action**,
  **Compensate**, **Re-enable**, **Delete policy**.
- Identifiers (doc keys, task keys, slot keys, event IDs) are monospace and
  never paraphrased.
- Status labels come from the server enum; use the server's status text rather
  than inventing friendlier names that obscure state.
- Error text states the cause and the recovery path. For example, a rebase
  conflict says the proposal base is stale and overlapping edits were not
  applied, instead of “something went wrong”.
- Do not claim a delivery, approval or execution succeeded until the server
  response reports it. Pending/approved/executing/succeeded are distinct.
- Do not fabricate counts, tenants, costs or benchmark numbers. The benchmark
  report is generated only from an actual run.
- Security copy is literal: tainted provenance, human verification, workspace
  isolation and approval gates are described as implemented.
- English and Simplified Chinese are both maintained in `dashboard/app/i18n.tsx`;
  every new UI string must be added to both copy sets.
- Technical identifiers and protocol terms (`doc://`, `TASK-*`, `Webhook`, JSON
  Schema, `ADMIN_TOKEN`) remain untranslated.
- Server errors are surfaced verbatim; they may be English even in the Chinese UI.
