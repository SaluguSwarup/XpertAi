# AI Interaction Log — Xpert.ai Winter Internship Challenge

## 1. Overview

This document transparently records how AI tools assisted throughout the analysis, implementation, debugging, refactoring, testing, and documentation of the **Xpert Cold Start** submission (`xpert-cold-start-starter/cold-start`).

The objective of the project is to extract structured personal-assistant entities (`people`, `organisations`, `chats`, `goals`, and `open_loops`) from WhatsApp-style chat histories (`chats.jsonl`, `messages.jsonl`, and `seed.json`) while strictly complying with `schema.json` and the challenge's single-command Linux execution contract (`./run.sh <input_dir> <output.json>` and `python3 solution.py`).

AI tools were used strictly as engineering, debugging, test-harness, and documentation assistants. Architectural choices, API/provider switches, quota limits, model selection, local dataset validation, and final submission approvals were directed and verified by the developer.

---

## 2. Tools and AI Assistance

### Development Assistants Evidenced in Local Records

1. **Antigravity AI Coding Agent (VS Code Workspace Session)**
   - **Evidence:** Local session transcript (`transcript.jsonl`) spanning `2026-11-04T17:11:45Z` through `2026-11-11T17:19:39Z` (`2026-11-04 22:41` – `2026-11-11 22:49 IST`).
   - **Scope of Assistance:** Challenge specification analysis, `check.py` / `schema.json` inspection, ingestion/indexing implementation, `extractor.py` prompt and validator authoring, batching design, unified CLI refactoring (`solution.py`), OpenRouter migration (`llm.py`, `solution.py`), checkpoint isolation and recovery fixes, offline mocked testing, and offline submission verification.

2. **OpenAI Codex Sandbox / CLI (`CodexSandboxOffline`)**
   - **Evidence:** `.git` directory ownership metadata (`CodexSandboxOffline`), Git commit `5691e5a4adff3c72d094ff03547d7e9d2d343d71` (`2026-11-11 22:25:10 +0530`), workspace documentation updates (`README.md`, `WRITEUP.md`, `.gitignore`), and the structured session review preserved in `ai-logs/README.md`.
   - **Scope of Assistance:** Documentation refinement (`README.md`, `WRITEUP.md`), `.gitignore` hardening for private answer keys and credentials, Git repository initialization with `run.sh` mode `100755`, and submission readiness review. Raw verbatim prompts from this tool are not stored in the repository (see Section 7).

### Target Inference Models Configured During Development Testing

*(These are the external LLM endpoints called by `llm.py` on chat-extraction inputs, not development agents.)*

- **`gemini-3.6-flash`** via Google AI Studio OpenAI-compatible endpoint (`25` calls recorded in `usage.jsonl` between `2026-11-05` and `2026-11-06`).
- **`nvidia/nemotron-3-ultra-550b-a55b:free`** via OpenRouter (`https://openrouter.ai/api/v1/chat/completions`, `2` calls recorded in `usage.jsonl` on `2026-11-11`).
- An additional OpenRouter free-tier attempt with a Gemma model resulting in HTTP `429` is referenced in `ai-logs/README.md` (not logged in `usage.jsonl` because failed HTTP attempts do not write usage records).

---

## 3. Chronological Interaction History

### Phase 1: Challenge Understanding & Starter Inspection (`2026-11-04 17:11Z` – `2026-11-05 01:10Z`)
- **Objective:** Understand the Cold Start problem, scoring metrics (`74` automated data points + `26` qualitative/cost/run points), input formats, and starter baseline.
- **Task:** Read `schema.json`, `check.py`, `run.sh`, `solution.py`, `example/`, and `data/dev/` schemas without modifying files.
- **Outcome:** Mapped `check.py` scoring rules (seed exclusion for circles/goals, organisation Jaccard matching, `source_msgs` matching, strict false-closure penalties, `loops_scored_from` closing window) and established the baseline naive score on `example/` (`12.4 / 74`).

### Phase 2: Data Ingestion, Indexing & Blind Circle Analysis (`2026-11-05 01:12Z` – `01:48Z`)
- **Objective:** Build clean input loading/indexing in `solution.py` and empirically evaluate circle-inference cues before touching `answers.json`.
- **Task:** Implement JSONL ingestion and lookup indexes (`message_by_id`, chronological `messages_by_chat_id`, `chats_by_participant_id`), verify seeded circles, and run blind predictions on the 41 unseeded labeled dev contacts.
- **Outcome:** Confirmed schema integrity across `example` (6 chats, 29 messages) and `data/dev` (138 chats, 4,502 messages, 286 non-`ME` participants) and clarified `12.4 / 74` score arithmetic (`1.7 + 2.7 + 8.0`).

### Phase 3: Modular Extraction & Conservative Validation Layer (`2026-11-05 01:54Z` – `02:18Z`)
- **Objective:** Build participant-level prompt generation and defensive validation isolated from network/CLI orchestration.
- **Task:** Implement `extractor.py` (`build_participant_context`, `create_extraction_prompt`, `parse_and_validate_extraction`, `is_unambiguous_closure`) plus an offline mock test script (`test_extract.py`, later removed during cleanup).
- **Outcome:** Verified offline that invalid IDs were stripped and unsupported/counterparty loop closures were reverted to `status: "open"` with `help: {"action": "leave", ...}` to avoid false-closure penalties.

### Phase 4: Gemini API Integration, Batching & CLI Streamlining (`2026-11-05 02:18Z` – `2026-11-06 11:14Z`)
- **Objective:** Connect `llm.py` to an OpenAI-compatible API, prevent free-tier rate-limit failures, and unify execution into `solution.py`.
- **Tasks & Outcomes:**
  - Added `.env` fallback loading (`_load_env_fallback`), retry backoff, and thread-safe `usage.jsonl` logging in `llm.py`.
  - Validated single-participant extraction on `example` (`P01 -> spouse`, exact match; `2,015` prompt + `654` completion tokens) and unseeded `data/dev` contacts (`P195`, `P238`, `P223`).
  - Observed unbatched 51-participant testing fail 47 calls with HTTP `429` (5 RPM limit) and 1 with HTTP `503`.
  - Implemented `build_batch_context` and `create_batch_prompt` in `extractor.py`, processing 20 dev participants in **1 batched call** (`327,224` prompt + `3,436` completion tokens, `0` errors, `55.0%` circle accuracy) saved to `geminiout.json`.
  - Refactored `solution.py` with `load_and_index`, `extract_and_consolidate`, `validate_output`, `evaluate_output`, `run_pipeline`, and CLI subcommands (`extract`, `check`, `evaluate`, `run`), reaching `49.0 / 74` on `example/` (Circles `12.0/12`, Chats `8.0/8`, Goals `8.0/8`) and removing temporary test scripts.
- **Developer Follow-up Run (`2026-11-06 ~16:57–17:06 IST`):** Local execution on `data/dev` populated `checkpoint.json` and `usage.jsonl` (rows 13–25), later revealing cross-dataset checkpoint contamination from stale `example` entries.

### Phase 5: Linux Portability, OpenRouter Migration & Checkpoint Isolation (`2026-11-11 15:34Z` – `17:05Z` / `21:04–22:35 IST`)
- **Objective:** Prepare the repository for independent clean-Linux evaluator execution and zero-cost OpenRouter testing.
- **Tasks & Outcomes:**
  - Audited execution architecture; identified that bare `python solution.py` exited at `--help`, `solution.py` hardcoded `model="gemini-3.6-flash"`, `cold-start/data` used a Windows NTFS junction, and `checkpoint.json` leaked `example` entries (`M001..M027`) into `data/dev`.
  - Updated `solution.py` so `python solution.py` executes `run_pipeline()` by default, honors `INPUT_DIR`/`OUTPUT_PATH` with CLI precedence, fails loudly when inputs are missing (no silent fallback to `example/`), sorts participants naturally (`_natural_sort_key`), adds `--limit` / `--fresh`, and isolates checkpoints under `.checkpoints/ckpt_<sig>.json` keyed by SHA-256 dataset and config hashes.
  - Updated `llm.py` and `solution.py` to default `LLM_BASE_URL` to `https://openrouter.ai/api/v1`, remove hardcoded `gemini-3.6-flash` overrides, strip/fallback `response_format={"type": "json_object"}` on HTTP `400` incompatibilities (`_should_fallback_without_json_mode`, `LLM_JSON_MODE`), limit transient retries (`DEFAULT_MAX_ATTEMPTS = 2`), abort immediately on hard quota `429`s, and log `"endpoint"` in `usage.jsonl`.
  - Replaced Windows NTFS junction `cold-start/data` with a standard directory copy and cleaned `.env` (preserving `LLM_API_KEY` unseen).
  - **Codex / Git Initialization & Live 10-Participant Test (`~21:40–22:32 IST`):** Updated `README.md`, `WRITEUP.md`, `.gitignore`, initialized Git (`5691e5a`, `run.sh` mode `100755`), and ran `--limit 10 --batch-size 2` with `nvidia/nemotron-3-ultra-550b-a55b:free` (Batch 1 `['P01', 'P02']` succeeded and saved `ckpt_f6e43643cde7caec.json`; Batch 2 `['P03', 'P04']` returned `3` completion tokens and failed with `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`).

### Phase 6: Failure Root-Cause Fix & Final Submission Audit (`2026-11-11 17:07Z` – `17:20Z` / `22:37–22:50 IST`)
- **Objective:** Resolve empty-response parsing and fallback checkpoint contamination, then perform final offline verification.
- **Tasks & Outcomes:**
  - Hardened `llm.py` (`_extract_message_content`, empty/null response `RuntimeError`, HTTP `200` error detection), `extractor.py` (explicit empty-response error, `<think>`-aware and non-greedy multi-fence extraction, `json.JSONDecodeError` before `ValueError`), and `solution.py` (never store fallbacks in `per_participant`, reject error/fallback/all-empty entries, abort cleanly preserving completed batches for retry).
  - Executed offline and mocked verification checks (syntax, example evaluation, CLI/schema checks, mocked failure/resume tests, `.gitignore`, `run.sh` mode `100755`).
  - Updated line 37 of `WRITEUP.md` so documentation matches the abort-and-resume failure behavior and inspected `git status` / `git diff`.

---

## 4. Representative Prompts

Below are representative prompts from the development record. Verbatim prompts come directly from `transcript.jsonl`; prompts outside that log are labeled as reconstructed summaries from `ai-logs/README.md`.

1. **Verbatim (2026-11-05 00:30Z — Scoring & Validation Inspection):**
   > "1. Show the actual scoring functions for open loops, loop status, and help decisions. 2. Explain exactly how answer-key records are matched to our output. 3. Identify any differences between schema validation and scoring requirements. 4. Point out which metrics we can test locally and which are unavailable because dev goals/help labels are missing."

2. **Verbatim (2026-11-05 02:05Z — Conservative Closure Policy):**
   > "Make one focused modification to `extractor.py` and `test_extract.py`. Currently, a closure with a valid `closed_by` message ID and a later timestamp is flagged for review, but the returned loop still retains `status: 'done'` or `'dropped'`. Update the conservative validation rule so that closures that are not clearly supported by subsequent messages are automatically reverted to `status: 'open'`..."

3. **Verbatim (2026-11-06 10:49Z — Batched Extraction Optimization):**
   > "Modify the extraction run to process these 20 participants and save their complete raw Gemini extraction outputs to `geminiout.json`, overwriting the existing file... **Batching requirement:** Try to include as many participants as possible in a single Gemini API request... Keep all ground-truth labels out of Gemini prompts; use them only for evaluation afterward."

4. **Verbatim (2026-11-11 15:34Z — Clean Linux Execution Architecture):**
   > "I want to restructure this repository so that the Xpert.ai evaluators can clone it and run it independently on their own clean Linux environment. For now, focus ONLY on understanding and establishing the expected execution architecture. Do not modify the extraction logic or implement new functionality yet."

5. **Verbatim (2026-11-11 15:49Z — Zero-Cost OpenRouter Readiness):**
   > "Make only the necessary code/configuration changes to prepare the OpenRouter integration for a strictly zero-cost test... Ensure JSON response formatting is handled safely. Do not assume every model supports `response_format={'type':'json_object'}`... Avoid unnecessary retries that could consume free-tier quota."

6. **Verbatim (2026-11-11 17:07Z — Empty JSON & Checkpoint Recovery Debugging):**
   > "Investigate and fix the malformed JSON extraction and checkpoint recovery issues from the recent first-10-participant run... Ensure failed extraction batches are not permanently treated as successfully completed."

7. **Reconstructed Summary (from `ai-logs/README.md`, 2026-11-11 ~21:40–22:32 IST — Codex Review & Submission Prep):**
   - Inspect model selection after encountering HTTP `429` on a Gemma free route, test `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter, tighten `.gitignore` (answer keys and credentials), update `README.md` / `WRITEUP.md` conservatively from observed `usage.jsonl` records, and verify Git state (`5691e5a`).

---

## 5. AI-Assisted Debugging and Problem Solving

| Issue Observed | Root Cause Identified | Fix Applied | Verification |
|---|---|---|---|
| `UnicodeEncodeError` (`cp1252`) printing chat names containing emojis (`❤️`, `🏠`) | Windows PowerShell default console encoding could not print UTF-8 chat titles | Configured UTF-8 stdout/stderr (`reconfigure(encoding="utf-8")`) guarded by `sys.platform == "win32"` in `solution.py` | Verified output on `example/` and `data/dev/` chats with emojis |
| HTTP `429` (47/51 failures) and `503` during sequential 1-per-participant Gemini calls | Free-tier 5 RPM limit exceeded when firing individual requests per participant | Implemented `build_batch_context` / `create_batch_prompt` to batch participants + exponential backoff retries | 20-participant batched extraction completed in 1 request with 0 errors |
| Cross-dataset checkpoint contamination (`M001..M027` errors on `data/dev` evaluation) | Legacy `checkpoint.json` keyed solely by `pid` (`P01..P08`) reused `example` goals across `data/dev` | Scoped `.checkpoints/ckpt_<sig>.json` by SHA-256 dataset & config hashes; enforced `target_pids` scoping and rejected legacy/mismatched checkpoints | Verified offline unit test rejected `example` checkpoints against `dataset_b` and mismatched models |
| Hardcoded `gemini-3.6-flash` override & Windows NTFS junction | `solution.py` passed `model="gemini-3.6-flash"` ignoring `LLM_MODEL`; `data` used a local NTFS junction; bare `python solution.py` printed help | Removed hardcoded model overrides, routed zero-arg `python solution.py` to `run_pipeline()`, replaced NTFS junction with standard directory, removed sibling workspace paths | Verified missing inputs fail (`exit 1`) without `example/` fallback; `LLM_MODEL` propagates cleanly in mocked runs |
| Batch 2 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` (`completion_tokens: 3`) & fallback saved on failure | OpenRouter returned empty/whitespace `content` (`completion_tokens=3`); `llm.py` did not check empty content; `solution.py` stored `"error"` fallbacks in `completed_extractions`; `ValueError` caught `json.JSONDecodeError` before snippet formatting | Validated `choices`, `message`, HTTP `200` errors, content lists, and non-empty text in `llm.py`; ordered `except json.JSONDecodeError` before `ValueError` and added `<think>`/multi-fence JSON extraction in `extractor.py`; changed `solution.py` to abort and preserve completed batches without writing fallbacks | Verified via isolated mocked HTTP tests covering empty content, malformed JSON, empty `{}`, and resume skipping `P01`/`P02` while retrying `P03`/`P04` |

---

## 6. Human Oversight and Verification

The developer maintained direct control over key engineering and submission boundaries:

- **Privacy & Credential Control:** API keys were created and managed externally in `.env` (gitignored); prompts and `.env` values were kept out of assistant outputs, and ground-truth `answers.json` entries were excluded from extraction prompts (`safe_seed` filtering).
- **Cost & Quota Governance:** When free-tier rate limits (`429`) and empty Nemotron responses (`completion_tokens=3`) surfaced, the developer halted live extraction immediately, required strict zero-cost checks (`:free` model IDs, reduced retries via `DEFAULT_MAX_ATTEMPTS = 2`, immediate abort on hard quota errors), and prohibited speculative live runs.
- **Code & Documentation Review:** The developer audited `check.py` scoring rules, verified `usage.jsonl` arithmetic (`27` records: `1,724,656` prompt + `32,811` completion tokens), required `WRITEUP.md` line 37 to be corrected when offline checks caught documentation drift against the updated checkpoint-abort logic, and enforced honest reporting of unverified accuracy on the hidden/full dev sets.
- **Independent Offline Verification Executed Without Live API Calls:**
  - `python -m py_compile solution.py extractor.py llm.py check.py` (`exit 0`).
  - `python check.py example out.json` and `python solution.py evaluate -i example -o out.json` (`12.4 / 74` on baseline `out.json`, `exit 0`).
  - CLI help checks, missing-input error checks (`exit 1` with explicit error), and `bash -n run.sh` + `./run.sh` arg validation (`exit 2`).
  - Mocked `urllib.request.urlopen` recovery test suite in a temporary directory verifying Batch 1 preservation (`P01`, `P02`), Batch 2 failure handling (empty content, malformed prose, empty `{}`), and clean resume retrying only `P03`, `P04`.
  - Git tracked/ignored audit verifying `.gitignore` exclusions and `run.sh` mode `100755`.

*(Note: While mocked tests and offline validation passed on the latest code, **no full end-to-end live OpenRouter extraction** has been completed across `data/dev` or the hidden test set after the latest edits.)*

---

## 7. Limitations

- **Incomplete Cross-Tool Transcript Coverage:** While the Antigravity session transcript (`transcript.jsonl`) captures full verbatim prompts and tool runs from `2026-11-04` to `2026-11-11`, the intermediate OpenAI Codex session (`2026-11-11 ~21:40–22:32 IST`, owned under `CodexSandboxOffline`) is evidenced only through Git history (`5691e5a`), file modifications, `usage.jsonl`, `ckpt_f6e43643cde7caec.json`, and the structured summary in `ai-logs/README.md`—its verbatim prompt text is unavailable and therefore summarized/reconstructed rather than quoted.
- **Unlogged Exploratory Console Output:** Manual local runs performed on `2026-11-06 ~16:57–17:06 IST` updated `usage.jsonl` (rows 13–25), `checkpoint.json`, and `output.json`, without captured stdout logs in the transcript; similarly, an aborted Gemma `:free` HTTP `429` attempt referenced in `ai-logs/README.md` left no entry in `usage.jsonl`.
- **Verification Scope:** Final verification on the modified failure-handling code used mocked HTTP responses and offline `example/` checks only; live extraction accuracy and runtime performance on OpenRouter models over the full `data/dev` or hidden evaluation set remain unmeasured until evaluated with live credentials.
- **Redactions:** All `.env` credentials, private dataset message bodies (`data/dev/`), and answer keys (`**/answers/`) are intentionally omitted from this log.

---

## 8. Final Summary

AI assistance supported this project across five concrete areas under human direction: (1) decomposing the Cold Start schema and scoring rules; (2) building and batching the modular extraction/validation pipeline (`extractor.py`, `solution.py`); (3) migrating and hardening the OpenAI-compatible HTTP client (`llm.py`) for OpenRouter, JSON-mode fallback, empty-response diagnostics, and conservative retries; (4) resolving cross-platform portability, `.gitignore` safety, and hash-isolated checkpoint recovery; and (5) building offline mock test harnesses and auditing `README.md` / `WRITEUP.md` consistency before final submission.
