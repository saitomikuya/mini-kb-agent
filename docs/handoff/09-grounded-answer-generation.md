# Handoff: 09 Grounded Answer Generation and Downloads

## Scope completed

- `QuestionAnsweringService` runs the complete navigation-to-answer flow.
  Navigation resolves `ModelRole.QUERY_ROUTER`; final generation separately
  resolves `ModelRole.ANSWER_GENERATION`.
- The answer model receives only the question, selected in-budget Markdown
  parts, trusted source metadata, and mandatory system rules. It receives no
  root/folder index, card path, Markdown path, source path, or router reason.
- The model output is strict JSON containing `answer_markdown`, `citations`,
  `conflicts`, and download intents. Model-side download items contain only a
  `document_id`; the public `AnswerResult` adds trusted `filename` and
  `relative_directory` values after database validation.
- Citation document/part/anchor/label tuples are checked against selected
  evidence. Conflict value provenance is checked against selected artifact
  anchors, and every valid conflicting value is retained in model order.
- System rules require evidence-only answers, explicit not-found responses,
  no common-knowledge completion, complete conflict display without averaging
  or silent choice, and explicit inference labeling for any recency-based
  correctness conclusion.
- `GET /api/files/{document_id}/download` returns the original authenticated
  source file with attachment disposition and Unicode filename support.

## Verification

- Full Python suite: `82 passed`.
- The required integration test persists `query_router=profile A` and
  `answer_generation=profile B`, executes one complete service call, and proves
  that routing requests reach only A while the final answer request reaches
  only B.

## Explicitly not implemented

- SSE;
- Chat UI or Chat API integration;
- Admin UI;
- Docker changes.
