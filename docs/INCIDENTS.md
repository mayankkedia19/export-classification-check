# Incident log

Real failures hit while building and testing this system (2026-10-12 to 2026-10-13). Each was found by a test, the
evaluation run or a live run, not invented for the log. Template for new entries at the bottom.

## INC-001: heading matcher produced an unsafe "clear-for-filing" (found by evaluation, case E17)
- **Symptom:** "Aluminium bicycle frame, road racing" was routed `clear-for-filing` under heading 9610 (slates and
  boards). Earlier in the same evaluation, "steel coat hook" was matched to 9616 (scent sprays) and a flask description
  matched 9602 (carved articles).
- **Detection:** `python -m eval.run` reported `unsafe_clear_rate = 0.125` and failing cases E07/E11/E17/E20.
- **Root cause:** heading terms were matched against the whole description, and qualifier words inside headings
  ("whether or not framed", "mounts", "moulded") counted as article names. Both agents share this heuristic, so the
  verifier agreed with the wrong answer.
- **Fix:** match only the article part of the description (before "with / for / made / ,"), drop qualifier clauses
  from heading phrases ("whether or not", "with", "for"), strip descriptive adjectives, make the stemmer consistent
  (sieve/sieves). `unsafe_clear_rate` went 0.125 -> 0.0.
- **Residual risk:** the heuristic is lexical and shared by both agents (common-mode failure). See "what did not work".

## INC-002: malformed model output at the first LLM call failed the whole run (found by test)
- **Symptom:** with the `malformed_json` fault injected, `test_malformed_model_output_is_repaired` failed: the run
  ended escalated with no proposal.
- **Root cause:** the research loop had a bounded repair retry, but the entity-extraction call before the loop did not.
- **Fix:** one bounded repair attempt on extraction. Test now passes.

## INC-003: PDF parser dropped every heading that followed a notes block (found by test)
- **Symptom:** a generated chapter PDF produced one giant "note" and zero headings.
- **Root cause:** after the `Section Notes` marker the parser stayed in notes mode and never recognised the next
  `9901 ...` heading line. Real tariff layouts put notes before headings, so this would have emptied the real corpus.
- **Fix:** a heading line ends notes mode. Regression test `test_pdf_text_layer_parsing`.
- **Caveat:** tested on a generated PDF only; the real CBIC layout was not available (egress blocked), so expect to
  tune `app/corpus/pdfparse.py` against the real files.

## INC-004: research loop re-issued the same search until the tool budget was exhausted (found by live trial)
- **Symptom:** "steel coat hook" ended `ToolBudgetExceeded` after two attempts and 14 tool calls.
- **Root cause:** the stub policy's "already searched" check compared a different query string from the one it issued,
  so it searched forever. The step/tool-call bounds (A1) did stop it and the run escalated instead of hanging.
- **Fix:** shared query constant; added the missing export-policy lookup on that branch.
- **Follow-up:** raised the default per-run tool budget from 14 to 30 after the v1 prompt (which needs a second attempt) hit
  the ceiling and degraded the verifier.

## Template
```
## INC-NNN: <title>
- Date/time (UTC):
- Symptom (what the user or dashboard saw):
- Detection (alert, metric, test, user report):
- Impact (runs affected, duration, wrong answers returned?):
- Root cause:
- Recovery / fix:
- Prevention (test, alert, runbook change):
```
