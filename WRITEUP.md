# Write-up

## Architecture and extraction

`solution.py` loads `chats.jsonl`, `messages.jsonl`, and an optional `seed.json`. It sorts messages in each chat by timestamp and builds indexes by message, chat, and participant. Participants are processed in deterministic ID order, either all at once, up to `--limit`, or as one requested participant. The default batch size is 10.

`extractor.py` builds a context containing the focus participant or batch, the chats in which they participate, and those chats' messages with IDs, timestamps, sender IDs/names, kind, body, and quoted-message ID. It asks the configured chat model for people, organisations, chat ties, goals, and open loops in JSON form. Seeded circles for focus participants are withheld from the extraction request; after extraction, seed circles take precedence in the final people list.

The response validator checks the model output against the input's person, chat, and message IDs and the allowed field values. It then `solution.py` aggregates the per-batch outputs. This is limited cross-chat consolidation: a participant's context can contain multiple chats, and a batch can share/deduplicate chats between focus participants. After calls, records are merged in code. Goals are deduplicated by lowercased title and capped at ten; loops are deduplicated by `source_msg`. There is no further global model call that reconciles the meaning of records across all batches, and duplicate detection is not semantic.

People use the challenge's relationship-circle categories. If no extracted circle is available, the finalizer uses `friend` as a schema-valid fallback. Organisations may be extracted from conversation context. Each group chat lacking a valid model tie receives a generated organisation record named from the chat; a one-to-one chat without a valid tie is assigned to a participant from its input record. These defaults guarantee a structural tie, not necessarily a correct classification.

## Goals, loops, and help decisions

The extractor requests goals with a title, area, type, horizon, and evidence message IDs. The implementation accepts at most ten after cross-batch title deduplication. It does not infer an evaluation score or confidence estimate.

A loop records its type, owner (`ME` or a person ID), chat, originating message, optional due date, status (`open`, `done`, or `dropped`), optional closure message, and optional help action. For a closed loop, the validator requires a known `closed_by` message and checks that the message is later than the source and is explicit, unambiguous closure evidence. The closure message must be from the loop owner or `ME`; when `ME` owns the task, a counterparty message alone does not prove closure. The explicit completion markers are a fixed list (for example, “done”, “paid”, and “submitted”), so valid closures phrased differently may be missed. Unsupported closures are reverted to open.

Help is retained only for open loops and may be `assist`, `remind`, or `leave`, with the corresponding optional details. If a loop was reverted to open because closure could not be established, the validator supplies a conservative `leave` default. The pipeline validates this field shape; it does not independently decide whether the model's help choice is appropriate.

## Model, requests, and cost

The final endpoint model must be configured through `LLM_MODEL` with the exact identifier accepted by the selected provider. No specific model ID is hardcoded as the final choice, and this repository contains no evidence that a particular model was successfully tested. `LLM_API_KEY` is required for API extraction. `LLM_BASE_URL` selects the OpenAI-compatible base URL and defaults to `https://openrouter.ai/api/v1`; the client calls its `/chat/completions` path. Process environment values override the optional local `.env` fallback.

The client requests JSON mode by default and can retry without that option when the endpoint rejects the response-format setting. Transient HTTP 429, 502, 503, and 504 errors may be retried; hard quota errors are not retried. The default is two total attempts, configurable with `LLM_MAX_RETRIES`. Calls use a 300-second timeout. Extraction batches are separated by a five-second delay.

Successful API responses append model, endpoint, prompt tokens, completion tokens, and elapsed seconds to `usage.jsonl`. The code does not record provider prices or calculate dollars. With provider rates quoted per million tokens, estimate cost as (prompt_tokens × input_rate + completion_tokens × output_rate) / 1,000,000, using the exact configured model's rates. No cost figure is claimed here. Batching multiple focus participants into a request and resuming completed batches can reduce repeated work, but larger contexts can increase token use or exceed a provider's context limit.

## Checkpoints, errors, and recovery

By default, batch extraction checkpoints are stored under `.checkpoints/`. The path is derived from hashes of the input files and extraction configuration. Checkpoints also store those hashes and version information; incompatible or unparsable files are ignored. Entries are checked against current IDs before reuse. Writes use a temporary file and atomic replacement. `--fresh` disables checkpoint reuse and saving for the run; `--checkpoint` can choose a path for the `extract` command. A single-participant extraction does not load or save the standard checkpoint.

A failed API request is reported and propagated. Invalid or unparsable model output is handled as a failed batch and does not contribute extracted items; the finalizer still emits deterministic structural fallbacks where it has them. File, configuration, URL, timeout, and JSON errors are reported by the CLI. The `check` command validates an existing output offline; the full `run` command validates after writing and evaluates only if an answer file exists.

## Limitations and unknowns

Model output can omit or misinterpret relationships, organisations, goals, loop owners, dates, or help decisions. Per-batch extraction means facts split across chats in different batches may not be reconciled by the model. Post-processing relies on exact IDs, source-message deduplication, and normalized goal-title equality rather than semantic matching. Fixed closure markers can miss paraphrases, while deterministic person/chat defaults can be wrong. Contexts include the messages from relevant chats without a source-level truncation strategy, so large chats may exceed model limits. No accuracy measurements, hidden-set results, model identity, provider pricing, or measured cost are available in this repository; none are asserted here.
