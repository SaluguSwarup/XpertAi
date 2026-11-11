# Xpert Cold Start

This repository contains a Python pipeline that reads WhatsApp-style chat data and produces people, organisation, chat, goal, and open-loop records. The repository root is the project root. Challenge inputs are supplied separately; the local `data/` directory is intentionally excluded from version control.

## Requirements

- Python 3.9 or newer
- Bash for `run.sh` (Linux/macOS)
- An API key and an OpenAI-compatible chat-completions endpoint for LLM extraction

The Python code uses only the standard library. No package installation is required. The evaluator must supply the exact model identifier accepted by its endpoint through `LLM_MODEL`; this repository does not pin or claim a tested model ID.

## Configuration

Set configuration in the process environment:

```bash
export LLM_API_KEY="<your-api-key>"
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_MODEL="<exact-model-id-required-by-your-endpoint>"
```

`LLM_API_KEY` and `LLM_MODEL` are required for API-backed extraction. `LLM_BASE_URL` defaults to `https://openrouter.ai/api/v1`. The client appends `/chat/completions` to the base URL. A local `.env` file is also read as a development fallback: existing process variables take precedence. Do not commit `.env` or credentials.

Optional settings include `INPUT_DIR`, `OUTPUT_PATH`, `LLM_MAX_RETRIES` (defaults to 2 total attempts), and `LLM_JSON_MODE` (set to `0`, `false`, `off`, or `no` to disable JSON response mode).

If no API key is configured, extraction calls are skipped and the pipeline writes deterministic fallback records. Those fallbacks are for exercising the output path; they are not equivalent to LLM extraction.

## Inputs

The input directory must contain:

```text
<input-dir>/
├── chats.jsonl       # Required
├── messages.jsonl    # Required
├── seed.json         # Optional
└── answers/answers.json  # Optional; used only by local evaluation
```

Each JSONL file contains one JSON object per non-empty line. The chat and message objects need the IDs and fields used by the pipeline; messages are grouped by `chat_id` and ordered by `ts`. The user is represented by sender/participant ID `ME`. `seed.json` may provide initial `circles`, `organisations`, and `goals`. Answer keys are not sent to the extraction model.

Input selection order is an explicit `-i/--input` argument (or positional input for `run`), then `INPUT_DIR`, then automatic discovery in the current directory, project directory, `input/`, `data/`, or `data/dev/`. The example fixture is not an automatic fallback; pass `-i example`. Missing required files cause an error.

## Run

From the repository root, with credentials configured and a supplied input directory:

```bash
./run.sh <input-dir> output.json
```

`run.sh` requires exactly two arguments: input directory and output path. Equivalent commands are:

```bash
python3 solution.py <input-dir> output.json
python3 solution.py run -i <input-dir> -o output.json
```

The CLI also supports automatic input discovery and the default output path `output.json`:

```bash
python3 solution.py
```

Useful development options include `extract --participant <person-id>`, `extract --limit <n>`, `extract --batch-size <n>`, and `--fresh` to ignore an existing checkpoint. Run `python3 solution.py --help` for CLI usage.

## Output and validation

The output is a JSON object with exactly these top-level arrays: `people`, `organisations`, `chats`, `goals`, and `open_loops`. Its structure and enums are defined in `schema.json`; inputs also constrain referenced person, chat, and message IDs. At most ten goals are emitted. LLM calls append token counts, model, endpoint, and elapsed seconds to `usage.jsonl`. This log is generated locally and is ignored by Git.

Validate output without making API calls:

```bash
python3 solution.py check -i <input-dir> -o output.json
# Equivalent standalone validator:
python3 check.py <input-dir> output.json
```

When `answers/answers.json` is present, evaluate the output locally with:

```bash
python3 solution.py evaluate -i <input-dir> -o output.json
```

The end-to-end `run` command validates output and evaluates it when an answer file is available. No accuracy or cost result is implied by these commands.

## Implementation map

- `solution.py`: CLI, input indexing, participant batching, aggregation, checkpointing, output, validation, and optional evaluation.
- `extractor.py`: participant/batch context construction, extraction requests, and response validation.
- `llm.py`: OpenAI-compatible HTTP client, retries, JSON-mode compatibility fallback, and usage logging.
- `check.py`: offline structural validation and development-set scoring.
- `schema.json`: output contract.
- `example/`: small input and answer fixture for local development.
