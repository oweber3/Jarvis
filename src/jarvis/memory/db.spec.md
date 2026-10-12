# Diary retrieval

`Database.search_hybrid` searches conversation summaries with FTS5 and optional
vector candidates. Both Python/FAISS and sqlite-vss use the same fusion routine.

- Candidate pools contain up to `max(top_k * 3, 50)` hits per source.
- Lower vector distance and more negative FTS5 BM25 rank first in their own lists.
- Weighted reciprocal rank fusion uses `0.6 / (60 + vector_rank)` plus
  `0.4 / (60 + keyword_rank)`. Ranks start at one; ties share a competition rank.
- A missing source contributes zero. Raw BM25 values and vector distances are
  never added or compared across sources.
- Only live summaries are returned, ordered by descending fused score then ID.
- Without a query vector, keyword results retain ascending BM25 order.
- Without a usable keyword query, the most recent summaries are returned.

`evals/test_hybrid_retrieval.py` measures lexical and semantic recall@3 on a
controlled corpus. It exercises ranking, not a particular embedding model.

## Summary rows and embeddings

- `upsert_conversation_summary` updates the row for a `(date_utc, source_app)` in
  place (`ON CONFLICT DO UPDATE`), so the row keeps its ID and the FTS update
  trigger keeps `summaries_fts` in step with it.
- `can_store_embeddings` is true when sqlite-vss is loaded or a FAISS or Python
  vector store is in use; every diary write path embeds when it is true and an
  embedding model is configured. A summary has at most one embedding:
  `upsert_summary_embedding` replaces the previous one for that ID.
- `delete_conversation_summary` (and `delete_summary_rows` for a plain
  connection such as the memory viewer's) removes the row, its FTS entry
  (delete trigger) and its stored embedding.

## Schema version

`Database` owns `PRAGMA user_version` of the file (`SCHEMA_VERSION`). On open,
a lower version is brought up once:

- Version 1 rebuilds `summaries_fts` from `conversation_summaries` and removes
  embeddings stored for IDs no summary has (left behind by rows that were
  replaced rather than updated).

The repair runs before the vector store loads, so only live embeddings are
loaded. A repair that fails (the file is busy, for example) is rolled back and
logged, the database opens as it is, and the repair runs at the next open. The
repair waits at most 250 ms for another connection's lock, then the
connection's usual busy timeout applies again.

## Activity sessions

The opt-in activity log keeps its `activity_sessions` table in the same SQLite
file, created and owned by `ActivityStore` (`memory/activity_log.spec.md`), not by
`Database`. It is never read by retrieval, the diary or the graph.

## Lists and notes

Lists, their items and quick notes keep `user_lists`, `user_list_items` and `user_notes` in the
same SQLite file, created and owned by `ListsNotesStore` (`memory/lists_notes.spec.md`), not by
`Database`. They are never read by retrieval, the diary or the graph.

## Web chat

The opt-in web chat keeps `chat_projects`, `chats`, `chat_messages` and `chat_state`
in the same SQLite file, created and owned by `ChatStore` (`webchat/webchat.spec.md`),
not by `Database`. Messages hold redacted text only. The tables are never read by
retrieval, the diary or the graph.
