# Handoff: 08 Query Navigation Only

## Scope completed

This implements only:

```text
user question -> current root JSON -> folder JSON -> document card -> Markdown part
```

- Phase 1 returns the strict Pydantic `FolderSelection` contract.
- Phase 2 returns the strict Pydantic `DocumentSelectionResult` contract.
- `NavigationService` resolves exactly `ModelRole.QUERY_ROUTER` and calls only
  `generate_json()`; it has no answer-generation dependency.
- Folder, document, and part ids are validated against parent-index
  whitelists before any selected artifact path is resolved.
- `NavigationResult` unifies intent, folders, documents, parts, public display
  steps, confidence, the clarification flag, and the calculated token budget.
- `display_reason` is treated only as a short public operation reason. The
  prompts do not request hidden chain-of-thought, and none is persisted.
- Defaults are configurable: eight selected documents and two logical
  navigation rounds.
- Root, folder, evidence, and output-reserve budgets derive from the selected
  query-router Model Profile. Large folder inputs use complete JSON batches;
  a large single document is structurally compressed/split by part metadata.
  JSON and Markdown content are never silently string-truncated.
- The Provider adapter prefers native Structured Outputs, then performs JSON
  prompt/parse fallback and exactly one malformed-JSON repair retry.

## Verification

- Full Python suite: `78 passed`.
- Tests cover correct folder selection, multiple folders, multiple documents,
  hallucinated document/part ids, the eight-document cap, malformed JSON,
  repair, context/evidence budgets, large-folder batching, and router/answer
  model isolation.

## Explicitly not implemented

- final answer generation;
- citations or conflict presentation;
- chat/SSE behavior;
- UI.
