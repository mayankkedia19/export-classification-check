# Corpus, chunking and embeddings (L1, D2)

## What is in the repository

`corpus/manifest.json` lists the sources. The only enabled source is `corpus/demo/passages.jsonl`: **DEMO content,
an approximate paraphrase written without access to the official publications** (every record has `official: false`
and a provenance note; clause numbers and wording must be checked). The service labels results from it
(`corpus_mode: "demo"`, a warning in every result, a banner in the UI) and never presents it as official legal text.
The DGFT export-policy entries are placeholders that say so.

The official sources are listed in the manifest with `enabled: false`:

| source_id | Document | publisher |
|---|---|---|
| `cbic-gri` | General Rules for the Interpretation | CBIC |
| `cbic-ch73` | Chapter 73 with Section XV and chapter notes | CBIC |
| `cbic-ch96` | Chapter 96 with chapter notes | CBIC |
| `dgft-itc` | ITC(HS) export policy, Chapters 73 and 96 | DGFT |

## Loading the official documents

1. Download the PDFs from CBIC and DGFT (public, free) into `corpus/official/` with the file names in the manifest.
2. Set each source's `version` (tariff year / policy date) and `"enabled": true`, and disable `demo-all`.
3. `python -m app.corpus.ingest` (or `docker compose run --rm indexer`).

The parser (`app/corpus/pdfparse.py`) reads the PDF text layer with pypdf and falls back to OCR (pypdfium2 + Tesseract)
when a page has no text layer. It recognises `NNNN heading` lines, numbered notes after a `Section/Chapter Notes`
marker, GRI rules and policy rows, keeps page numbers, and extracts "Chapter NN" references from exclusion notes into
`excludes_chapters`. **It was tested on a generated PDF only, because the official files could not be downloaded in the
build environment; expect to adjust the regular expressions to the real layout.** The ingestion log lists every
passage skipped and the manifest records each file's SHA-256.

## Chunking strategy and the section-3 difficulty

The corpus is hard because some headings describe goods by material and others by what the article is, and notes
override headings. So:

- **One chunk per legal unit** (a heading, a numbered note, a GRI rule). Units over 1100 characters are split on
  `; `, `. `, `, ` boundaries with overlap, keeping the unit's identity (`passage_id#n`, `parent_id`).
- **Legal context travels with the text.** The embedded string is `title | Section | clause | heading title | text`, so a
  note is never embedded without the chapter/section it belongs to, and a heading chunk carries its chapter.
- **Metadata is first-class**: source, version, page, clause, chapter, section, heading code, `applies_to_chapters`,
  `excludes_chapters`, `official`. Citations carry it back to the user.
- **Heading names are queryable.** Alongside similarity search, the `search_heading_names` tool tests whether a
  heading's own enumerated article names ("vacuum flasks", "nails") occur in the described article. This finds the
  heading the request never resembles semantically.

## Embeddings and index

- Local only; no paid API. `EMBEDDING_BACKEND=auto` uses sentence-transformers when `EMBEDDING_MODEL` is set and the
  package/model is available, otherwise a deterministic **hashing embedder** (stemmed word + bigram features, 2048
  dims). The hashing embedder is lexical, not semantic; it exists so the system runs offline. Set the model recommended in
  assignment section 12 and rebuild (`--reembed`) for real dense embeddings (`docker compose build --build-arg WITH_ST=1`).
- FAISS `IndexIDMap(IndexFlatIP)` over normalised vectors plus an in-memory BM25; search is hybrid by default
  (reciprocal-rank fusion), with `semantic` and `lexical` modes.
- **Idempotent and incremental.** Chunk id = hash(source, version, passage id, embedded text). A re-run embeds only chunks whose id
  is new, drops removed ones and reuses stored vectors for the rest; `--reembed-source ID` re-embeds one source and
  `--reembed` everything (needed after changing the embedding model, which is detected automatically);
  `--rebuild` starts from scratch.
- **Freshness** (`GET /v1/corpus/status`, metrics): build time, source versions and hashes, document and chunk counts,
  official-chunk ratio, chunks embedded per second.
