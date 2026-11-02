# Knowledge ingestion cutover

The `knowledge_ingest_policy` row for `douyin` records the source post/comment ID
high-water marks at cutover. Existing raw records are `legacy_skipped` by policy;
completed knowledge results remain intact. Pending/failed historical tasks and
pending historical topic work are explicitly skipped. This is an operational
skip, not a claim that model extraction succeeded.

The rotating daily scan never goes below the post floor. Batch submission and
manual post/comment processing also respect this boundary. Independent evidence
lookup excludes the legacy corpus after cutover. Existing published knowledge
and source records are retained; no raw content is deleted.

New Douyin posts must have the original numeric `aweme_id`. New comment exports
must include `aweme_id` (or `帖子ID` / `作品ID`) as an exact text value, shared with
the parent post. Titles are display fields. Missing IDs and floating-point /
scientific-notation IDs are rejected; an import error does not archive its files.
The source collector/exporter must supply this field; the importer cannot recover
it safely from a title. Comment deduplication includes the parent aweme ID.

`adapters.comment_data_adapter.ensure_douyin_raw_comments_table` adds the nullable
indexed `aweme_id` column for existing databases. `db_sync_service` migrates the
source and target comment schema when synchronizing and preserves the new field.
The independent SQL export tool is for newly created tables; use the adapter to
migrate an existing table before executing its generated INSERT statements.

## Apply once

Stop the knowledge timers, check no jobs are running, deploy this code, then run:

```bash
.venv/bin/python scripts/cutover_knowledge_ingestion.py
.venv/bin/python scripts/cutover_knowledge_ingestion.py --apply
```

The script takes the same MySQL advisory lock as the daily task, records a private
JSON status backup under `var/deploy_backups/`, and updates policy, queues and
cursor in a transaction. Repeating it returns `already_applied` without moving
the boundary or taking another backup. Re-enable the previously active timers.

## Rollback

Stop both timers and confirm no knowledge jobs are running. Retain any records
created after cutover. Under the knowledge production lock, restore only the
backed-up IDs' status fields in `knowledge_tasks` and `knowledge_comment_items`,
restore the backed-up topic status fields and scan cursor, then delete the
`douyin` row from `knowledge_ingest_policy` in the same transaction. Keep the
additive nullable raw-comment ID column. Roll back the code commit if required,
then restore the timers. Do not replace whole tables or delete new records.
