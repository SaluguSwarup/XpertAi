#!/usr/bin/env python3
"""Unified challenge pipeline and CLI for Xpert Cold Start.

Commands:
    python solution.py                             Run complete pipeline (default)
    python solution.py extract                     Extract and generate output.json
    python solution.py extract --participant P01   Extract for a single participant
    python solution.py check                       Validate output.json offline
    python solution.py evaluate                    Evaluate against answers/answers.json
    python solution.py run                         Execute complete pipeline
    python solution.py <input_dir> <output.json>   Standard runner (run.sh compatibility)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
from pathlib import Path

# Ensure repository directory is on python path
cold_start_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(cold_start_dir))

# Force UTF-8 encoding on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import check
import extractor
import llm


REQUIRED_INPUT_FILES = ("chats.jsonl", "messages.jsonl")


def resolve_configured_input(input_arg=None):
    """Return explicitly configured input path (CLI arg or INPUT_DIR env var), or None."""
    if input_arg is not None:
        cleaned = str(input_arg).strip()
        if not cleaned:
            raise ValueError("Input directory path cannot be empty.")
        return cleaned
    llm._load_env_fallback()
    env_input = os.environ.get("INPUT_DIR")
    return env_input.strip() if env_input and env_input.strip() else None


def resolve_configured_output(output_arg=None):
    """Return configured output path (CLI arg, OUTPUT_PATH env var, or 'output.json')."""
    if output_arg is not None:
        cleaned = str(output_arg).strip()
        if not cleaned:
            raise ValueError("Output file path cannot be empty.")
        return cleaned
    llm._load_env_fallback()
    env_output = os.environ.get("OUTPUT_PATH")
    return env_output.strip() if env_output and env_output.strip() else "output.json"


def _validate_input_files(folder):
    """Verify that required challenge input files exist in folder."""
    if not folder.exists():
        raise FileNotFoundError(f"Input directory does not exist: {folder}")
    if not folder.is_dir():
        raise FileNotFoundError(f"Input path is not a directory: {folder}")
    missing = [name for name in REQUIRED_INPUT_FILES if not (folder / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing required input file(s) in {folder}: {', '.join(missing)}"
        )
    return folder.resolve()


def resolve_input_dir(input_arg=None, output_path=None):
    """Locate and validate the dataset directory from explicit path, INPUT_DIR, or defaults.

    Never silently falls back to the example dataset when explicit inputs are missing
    or when default data locations are unpopulated.
    """
    explicit = resolve_configured_input(input_arg)
    if explicit is not None:
        return _validate_input_files(Path(explicit))

    candidates = [
        Path.cwd(),
        cold_start_dir,
        Path("input"),
        cold_start_dir / "input",
        Path("data"),
        cold_start_dir / "data",
        Path("data/dev"),
        cold_start_dir / "data" / "dev",
    ]
    checked = []
    for c in candidates:
        resolved_c = c.resolve()
        if resolved_c not in checked:
            checked.append(resolved_c)
        if all((c / name).is_file() for name in REQUIRED_INPUT_FILES):
            return resolved_c

    raise FileNotFoundError(
        "Could not locate required input files (chats.jsonl, messages.jsonl). "
        "Pass --input <dir> (e.g. -i example or -i data/dev), set INPUT_DIR, or place inputs in ./, ./input/, or ./data/. "
        f"Checked: {', '.join(str(p) for p in checked)}"
    )


def read_jsonl(path):
    """Read a JSON-lines file."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_and_index(input_dir):
    """
    Load chats, messages, and seed data from the input directory.
    Build fast lookup indexes while preserving all original fields and chronological ordering.
    """
    folder = _validate_input_files(Path(input_dir))
    chats = read_jsonl(folder / "chats.jsonl")
    messages = read_jsonl(folder / "messages.jsonl")
    seed_path = folder / "seed.json"
    if not seed_path.exists():
        print(f"[WARNING] Optional seed.json not found in {folder}; using empty seed.")
        seed = {}
    else:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))

    # Index 1: message ID -> message dict
    message_by_id = {m["id"]: m for m in messages}

    # Index 2: chat ID -> chronologically sorted messages
    messages_by_chat_id = {c["chat_id"]: [] for c in chats}
    for m in messages:
        messages_by_chat_id.setdefault(m["chat_id"], []).append(m)
    for msgs in messages_by_chat_id.values():
        msgs.sort(key=lambda m: m.get("ts", 0))

    # Index 3: participant ID -> list of chats they participate in
    chats_by_participant_id = {}
    for c in chats:
        for pid in c.get("participants", []):
            chats_by_participant_id.setdefault(pid, []).append(c)

    # Diagnostic integrity verification
    total_indexed_msgs = sum(len(msgs) for msgs in messages_by_chat_id.values())
    assert total_indexed_msgs == len(messages), (
        f"Indexing error: {len(messages)} messages loaded, but {total_indexed_msgs} indexed"
    )

    return chats, messages, seed, message_by_id, messages_by_chat_id, chats_by_participant_id


def _natural_sort_key(identifier):
    """Sort IDs deterministically (e.g. P01, P02, ..., P10, P11)."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(identifier))]


def _compute_dataset_hash(input_dir):
    """Compute a path-independent SHA-256 fingerprint of dataset input files."""
    h = hashlib.sha256()
    folder = Path(input_dir)
    for name in ("chats.jsonl", "messages.jsonl", "seed.json"):
        p = folder / name
        h.update(name.encode("utf-8"))
        if p.is_file():
            h.update(b":present:")
            h.update(str(p.stat().st_size).encode("utf-8"))
            h.update(b":")
            h.update(p.read_bytes())
        else:
            h.update(b":missing:")
    return h.hexdigest()


def _compute_config_hash(selected_model, selected_base_url, has_api_key, participant, limit, batch_size):
    """Compute a SHA-256 fingerprint of the active extraction configuration."""
    config_obj = {
        "version": 2,
        "model": selected_model if has_api_key else "__offline_baseline__",
        "base_url": selected_base_url if has_api_key else "",
        "participant": participant,
        "limit": limit,
        "batch_size": batch_size,
    }
    encoded = json.dumps(config_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), config_obj


def resolve_checkpoint_path(checkpoint_file, run_signature):
    """Resolve an isolated checkpoint path scoped by dataset and extraction config."""
    if checkpoint_file:
        return Path(checkpoint_file).resolve()
    return (cold_start_dir / ".checkpoints" / f"ckpt_{run_signature}.json").resolve()


def _load_valid_checkpoint(checkpoint_path, dataset_hash, config_hash, target_pids,
                           valid_chat_ids, valid_person_ids, valid_msg_ids):
    """Load checkpoint entries only when dataset and config hashes match and IDs are valid."""
    if not checkpoint_path.exists():
        return {}
    try:
        ckpt_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARNING] Could not parse checkpoint '{checkpoint_path.name}' ({e}); ignoring stale file.")
        return {}

    if (
        not isinstance(ckpt_data, dict)
        or ckpt_data.get("version") != 2
        or ckpt_data.get("dataset_hash") != dataset_hash
        or ckpt_data.get("config_hash") != config_hash
    ):
        print(
            f"[INFO] Checkpoint '{checkpoint_path.name}' belongs to a different dataset or configuration; "
            "ignoring stale entries."
        )
        return {}

    raw_per_p = ckpt_data.get("per_participant")
    if not isinstance(raw_per_p, dict):
        return {}

    failed_set = set(ckpt_data.get("failed_participants") or [])
    required_vd_keys = ("people", "organisations", "chats", "goals", "open_loops")

    loaded = {}
    for pid in target_pids:
        if pid in failed_set:
            continue
        entry = raw_per_p.get(pid)
        if not isinstance(entry, dict):
            continue
        if entry.get("error") or entry.get("is_fallback"):
            continue
        if entry.get("status") not in (None, "success", "completed"):
            continue
        vd = entry.get("val_data")
        if not isinstance(vd, dict) or not all(isinstance(vd.get(k), list) for k in required_vd_keys):
            continue
        if entry.get("circle") is None and not any(vd.get(k) for k in required_vd_keys):
            continue
        chat_ok = all(c.get("chat_id") in valid_chat_ids for c in vd.get("chats", []) if isinstance(c, dict))
        people_ok = all(p.get("id") in valid_person_ids for p in vd.get("people", []) if isinstance(p, dict))
        loop_ok = all(l.get("source_msg") in valid_msg_ids for l in vd.get("open_loops", []) if isinstance(l, dict))
        if chat_ok and people_ok and loop_ok:
            loaded[pid] = entry
    return loaded


def _save_checkpoint_atomic(checkpoint_path, dataset_hash, config_hash, config_obj,
                            completed_extractions, failed_participants=None):
    """Atomically write verified successful checkpoint entries without storing fallbacks."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    clean_per_p = {
        pid: entry
        for pid, entry in completed_extractions.items()
        if isinstance(entry, dict)
        and not entry.get("error")
        and not entry.get("is_fallback")
        and entry.get("status", "success") in ("success", "completed")
    }
    payload = {
        "version": 2,
        "dataset_hash": dataset_hash,
        "config_hash": config_hash,
        "config": config_obj,
        "per_participant": clean_per_p,
    }
    if failed_participants:
        payload["failed_participants"] = sorted(set(failed_participants))
    tmp_path = checkpoint_path.with_name(f".{checkpoint_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, checkpoint_path)


def extract_and_consolidate(input_dir=None, output_path=None, participant=None,
                            limit=None, checkpoint_file=None, batch_size=10,
                            use_checkpoint=True):
    """
    Run extraction and cross-chat open-loop consolidation.
    Supports single-participant, limited (--limit N), and batched multi-participant extraction.
    Reads LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL (with .env fallback), and isolates checkpoints.
    """
    if limit is not None and limit <= 0:
        raise ValueError("--limit must be a positive integer.")
    if batch_size is None or batch_size <= 0:
        raise ValueError("--batch-size must be a positive integer.")

    llm._load_env_fallback()
    output_path = resolve_configured_output(output_path)
    input_dir = resolve_input_dir(input_dir)
    print(f"Loading and indexing data from: {input_dir}")
    chats, messages, seed, message_by_id, messages_by_chat_id, chats_by_participant_id = load_and_index(input_dir)

    valid_chat_ids = {c["chat_id"] for c in chats}
    valid_person_ids = {p for c in chats for p in c.get("participants", [])} - {"ME"}
    valid_msg_ids = {m["id"] for m in messages}
    seeded_circles = seed.get("circles", {}) if isinstance(seed, dict) else {}

    # Target participants in deterministic order
    if participant:
        if participant not in valid_person_ids:
            print(f"[WARNING] Participant '{participant}' not found in chats. Known: {sorted(valid_person_ids, key=_natural_sort_key)[:5]}...")
        target_pids = [participant]
    else:
        ordered_pids = sorted(valid_person_ids, key=_natural_sort_key)
        target_pids = ordered_pids[:limit] if limit is not None else ordered_pids

    print(f"Target participants to extract: {len(target_pids)} ({target_pids[:10]}{'...' if len(target_pids) > 10 else ''})")

    has_api_key = bool((os.environ.get("LLM_API_KEY") or "").strip())
    selected_model = (os.environ.get("LLM_MODEL") or llm.DEFAULT_MODEL or "").strip()
    selected_base_url = (os.environ.get("LLM_BASE_URL") or llm.DEFAULT_BASE_URL).strip().rstrip("/")
    if not has_api_key:
        print("[WARNING] LLM_API_KEY is not configured in .env or environment.")
        print("          Falling back to deterministic baseline extraction.")

    dataset_hash = _compute_dataset_hash(input_dir)
    config_hash, config_obj = _compute_config_hash(
        selected_model, selected_base_url, has_api_key, participant, limit, batch_size
    )
    run_signature = hashlib.sha256(f"{dataset_hash}:{config_hash}".encode("utf-8")).hexdigest()[:16]
    checkpoint_path = resolve_checkpoint_path(checkpoint_file, run_signature)

    completed_extractions = {}
    if not participant and use_checkpoint:
        completed_extractions = _load_valid_checkpoint(
            checkpoint_path, dataset_hash, config_hash, target_pids,
            valid_chat_ids, valid_person_ids, valid_msg_ids
        )
        if completed_extractions:
            print(f"Resuming from checkpoint '{checkpoint_path.name}' ({len(completed_extractions)}/{len(target_pids)} target participants completed).")

    extracted_people = {}
    extracted_orgs = {}
    extracted_chats = {}
    extracted_goals = []
    extracted_loops = []

    # Filter out already completed target participants
    pending_pids = [pid for pid in target_pids if pid not in completed_extractions]

    if has_api_key and pending_pids:
        if not selected_model:
            raise ValueError(
                "LLM_MODEL environment variable is not set. "
                "Please set LLM_MODEL (via environment variable or .env) before running extraction."
            )
        # Group pending participants into batches
        batches = [pending_pids[i:i + batch_size] for i in range(0, len(pending_pids), batch_size)]
        print(f"Using LLM endpoint: {selected_base_url} | model: {selected_model}")
        print(f"Processing {len(pending_pids)} pending participants in {len(batches)} batch request(s)...")

        for b_idx, batch_pids in enumerate(batches, 1):
            print(f"  [Batch {b_idx}/{len(batches)}] Extracting for {len(batch_pids)} participants: {batch_pids[:10]}...")
            t0 = time.time()
            try:
                # Keep ground truth circle labels strictly out of prompt
                safe_seed = {k: v for k, v in seeded_circles.items() if k not in batch_pids}
                if len(batch_pids) == 1:
                    ctx = extractor.build_participant_context(batch_pids[0], chats_by_participant_id, messages_by_chat_id)
                else:
                    ctx = extractor.build_batch_context(batch_pids, chats_by_participant_id, messages_by_chat_id)

                raw_res = extractor.call_llm_extractor(ctx, seeded_circles=safe_seed, model=selected_model)
                val_data, errs, flags = extractor.parse_and_validate_extraction(
                    raw_res,
                    valid_chat_ids=valid_chat_ids,
                    valid_person_ids=valid_person_ids,
                    valid_msg_ids=valid_msg_ids,
                    messages_by_id=message_by_id,
                    require_explicit_closure=True
                )
                if val_data is None:
                    raise ValueError(f"Extraction response could not be parsed as valid JSON: {errs}")
                if not any(val_data.get(k) for k in ("people", "organisations", "chats", "goals", "open_loops")):
                    raise ValueError("Model returned an empty extraction JSON object with zero extracted entities.")
                elapsed = round(time.time() - t0, 2)
                print(f"    Success in {elapsed}s: {len(val_data.get('people', []))} people, {len(val_data.get('open_loops', []))} loops.")

                # Record strictly verified extractions in completed_extractions
                for pid in batch_pids:
                    p_circle = next((p["circle"] for p in val_data.get("people", []) if p["id"] == pid), None)
                    completed_extractions[pid] = {
                        "status": "success",
                        "circle": p_circle,
                        "val_data": val_data,
                        "elapsed": elapsed
                    }

                # Intermediate atomic checkpoint save (never persists failed batches)
                if not participant and use_checkpoint:
                    _save_checkpoint_atomic(checkpoint_path, dataset_hash, config_hash, config_obj, completed_extractions)
                    print(f"    [CHECKPOINT] Saved {checkpoint_path}")

                # Pacing delay between batches
                if b_idx < len(batches):
                    time.sleep(5)

            except (RuntimeError, urllib.error.URLError, TimeoutError, ValueError) as e:
                print(
                    f"    [EXTRACTION ERROR] Batch {b_idx} ({batch_pids}) failed ({e}). "
                    f"Skipping silent fallback; {len(completed_extractions)} completed participant(s) preserved for retry.",
                    file=sys.stderr,
                )
                if not participant and use_checkpoint and completed_extractions:
                    _save_checkpoint_atomic(
                        checkpoint_path,
                        dataset_hash,
                        config_hash,
                        config_obj,
                        completed_extractions,
                        failed_participants=batch_pids,
                    )
                raise RuntimeError(f"Batch {b_idx} ({batch_pids}) extraction failed: {e}") from e

    # Aggregate extracted items strictly for verified target_pids
    for pid in target_pids:
        res = completed_extractions.get(pid)
        if not res or res.get("error") or res.get("is_fallback"):
            continue
        vd = res.get("val_data", {})
        if res.get("circle"):
            extracted_people[pid] = {"id": pid, "circle": res["circle"]}
        for p in vd.get("people", []):
            if p["id"] not in extracted_people:
                extracted_people[p["id"]] = p
        for o in vd.get("organisations", []):
            extracted_orgs[o["id"]] = o
        for c in vd.get("chats", []):
            extracted_chats[c["chat_id"]] = c
        for g in vd.get("goals", []):
            extracted_goals.append(g)
        for l in vd.get("open_loops", []):
            extracted_loops.append(l)

    # -------------------------------------------------------------
    # Cross-chat Open-loop & Schema Consolidation
    # -------------------------------------------------------------
    # 1. People: Seed circles take absolute precedence
    final_people = []
    for pid in sorted(valid_person_ids, key=_natural_sort_key):
        if pid in seeded_circles:
            circle = seeded_circles[pid]
        elif pid in extracted_people and extracted_people[pid].get("circle"):
            circle = extracted_people[pid]["circle"]
        else:
            circle = "friend"  # Schema-valid baseline fallback
        final_people.append({"id": pid, "circle": circle})

    # 2. Organisations: Ensure every group chat has an organisation
    final_orgs = list(extracted_orgs.values())
    org_ids = {o["id"] for o in final_orgs}
    final_chats = []
    tied_chats = set()

    for c in chats:
        cid = c["chat_id"]
        # Use LLM tie if available and valid
        if cid in extracted_chats:
            tie = extracted_chats[cid]
            if "organisation" in tie and tie["organisation"] in org_ids:
                final_chats.append(tie)
                tied_chats.add(cid)
                continue
            elif "person" in tie and tie["person"] in valid_person_ids:
                final_chats.append(tie)
                tied_chats.add(cid)
                continue

        # Default tie policy: 1:1 -> person, Group -> organisation
        if c["is_group"]:
            default_org = "org_" + cid
            if default_org not in org_ids:
                final_orgs.append({
                    "id": default_org,
                    "name": c["name"],
                    "type": "other",
                    "side": "work",
                    "role": "member"
                })
                org_ids.add(default_org)
            final_chats.append({"chat_id": cid, "organisation": default_org})
        else:
            other = next((p for p in c.get("participants", []) if p != "ME"), None)
            if other and other in valid_person_ids:
                final_chats.append({"chat_id": cid, "person": other})
            else:
                fallback_person = sorted(valid_person_ids, key=_natural_sort_key)[0]
                final_chats.append({"chat_id": cid, "person": fallback_person})
        tied_chats.add(cid)

    # 3. Goals: Deduplicate and cap at 10
    final_goals = []
    seen_goal_titles = set()
    for g in extracted_goals:
        title = g.get("title", "").strip().lower()
        if title and title not in seen_goal_titles:
            seen_goal_titles.add(title)
            final_goals.append(g)
            if len(final_goals) == 10:
                break

    # 4. Open Loops: Deduplicate by source_msg, enforce conservative closure
    final_loops = []
    seen_source_msgs = set()
    for l in extracted_loops:
        src = l.get("source_msg")
        if src in valid_msg_ids and src not in seen_source_msgs:
            seen_source_msgs.add(src)
            final_loops.append(l)

    output_data = {
        "people": final_people,
        "organisations": final_orgs,
        "chats": final_chats,
        "goals": final_goals,
        "open_loops": final_loops
    }

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[DONE] Successfully wrote challenge-compatible output to {out_p.resolve()}")
    print(f"  People        : {len(final_people)}")
    print(f"  Organisations : {len(final_orgs)}")
    print(f"  Chats tied    : {len(final_chats)}")
    print(f"  Goals         : {len(final_goals)}")
    print(f"  Open loops    : {len(final_loops)}")

    return output_data


def validate_output(output_path=None, input_dir=None):
    """Validate output against schema and input constraints without making API calls."""
    output_path = resolve_configured_output(output_path)
    input_dir = resolve_input_dir(input_dir, output_path)
    chats = read_jsonl(input_dir / "chats.jsonl")
    messages = read_jsonl(input_dir / "messages.jsonl")

    out_file = Path(output_path)
    if not out_file.exists():
        print(f"[ERROR] Output file not found: {out_file}")
        return False

    try:
        out = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] Could not parse JSON from {output_path}: {e}")
        return False

    errs = check.validate(out, chats, messages)
    if errs:
        print(f"\n[VALIDATION FAILED] Found {len(errs)} issue(s) in {output_path}:")
        for e in errs[:25]:
            print(f"  - {e}")
        if len(errs) > 25:
            print(f"  ... and {len(errs) - 25} more")
        return False

    print(f"\n[CHECK PASSED] The output in {output_path} is completely schema-valid.")
    return True


def evaluate_output(output_path=None, input_dir=None):
    """Evaluate output.json against the development answer key."""
    output_path = resolve_configured_output(output_path)
    input_dir = resolve_input_dir(input_dir, output_path)
    chats, messages, seed, answers = check.load_input(input_dir)

    out_file = Path(output_path)
    if not out_file.exists():
        print(f"[ERROR] Output file not found: {out_file}")
        return 1

    try:
        out = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] Could not read {output_path}: {e}")
        return 1

    # First validate shape
    errs = check.validate(out, chats, messages)
    if errs:
        print(f"[VALIDATION FAILED] Output has {len(errs)} format errors. Cannot score invalid output.")
        for e in errs[:10]:
            print("  -", e)
        return 1

    if answers is None:
        print(f"No answers/answers.json found in {input_dir}. Nothing to score against.")
        return 0

    scores = check.score(out, answers, seed, messages)
    print("\n======================================================================")
    print("EVALUATION AGAINST DEVELOPMENT ANSWER KEY")
    print("======================================================================")
    print(f"{'Category':<26}{'Score':>8}{'Points':>12}")
    print("-" * 48)

    labels = {
        "circles": "Circles",
        "organisations": "Organisations",
        "chats": "Chats",
        "goals": "Goals",
        "loops_found": "Open loops found",
        "loops_closed": "Open loops closed right",
        "help": "Where to help",
    }

    total = 0.0
    possible = 0
    for key, pts in check.POINTS.items():
        if scores[key] is None:
            print(f"{labels[key]:<26}{'not labelled':>20}")
            continue
        got = scores[key] * pts
        total += got
        possible += pts
        print(f"{labels[key]:<26}{scores[key]:>8.2f}{got:>8.1f} / {pts}")

    print("-" * 48)
    print(f"{'Total Score from Data':<26}{'':>8}{total:>8.1f} / {possible}")
    print("======================================================================")
    return 0


def _first_not_none(*values):
    """Return the first value that is not None (preserves empty strings for validation)."""
    for v in values:
        if v is not None:
            return v
    return None


def run_pipeline(input_dir=None, output_path=None, limit=None, batch_size=10, use_checkpoint=True):
    """Execute complete end-to-end pipeline."""
    input_dir = resolve_input_dir(input_dir)
    output_path = resolve_configured_output(output_path)
    print("======================================================================")
    print("RUNNING COMPLETE COLD START PIPELINE")
    print("======================================================================")
    print(f"Input Directory  : {input_dir}")
    print(f"Output File      : {output_path}")

    # 1. Extraction & Consolidation
    extract_and_consolidate(
        input_dir, output_path, limit=limit, batch_size=batch_size, use_checkpoint=use_checkpoint
    )

    # 2. Offline Validation
    valid = validate_output(output_path, input_dir)
    if not valid:
        print("[PIPELINE HALTED] Validation failed.")
        return 1

    # 3. Evaluation if answers available
    answers_path = input_dir / "answers" / "answers.json"
    if answers_path.exists():
        evaluate_output(output_path, input_dir)

    print("\n[COMPLETE] Pipeline execution completed successfully.")
    return 0


def build_cli():
    """Build the unified command line interface."""
    parser = argparse.ArgumentParser(
        description="Xpert Cold Start Life-Understanding Pipeline CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python solution.py
  python solution.py run
  python solution.py extract --limit 10 -i data/dev -o first10_out.json
  python solution.py extract --participant P01 -i example -o test_or_out.json
  python solution.py check -i data/dev -o first10_out.json
  python solution.py evaluate -i data/dev -o first10_out.json
  python solution.py data/dev output.json
"""
    )
    parser.add_argument("-i", "--input", dest="root_input", default=None, help="Input dataset directory")
    parser.add_argument("-o", "--output", dest="root_output", default=None, help="Output file path")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands (omit to run full pipeline)")

    # Command: extract
    p_extract = subparsers.add_parser("extract", help="Run extraction and generate output.json")
    p_extract.add_argument("-i", "--input", default=None, help="Input dataset directory")
    p_extract.add_argument("-o", "--output", default=None, help="Output file path (default: OUTPUT_PATH or output.json)")
    p_extract.add_argument("-p", "--participant", default=None, help="Extract for one specific participant")
    p_extract.add_argument("-l", "--limit", type=int, default=None, help="Extract only the first N participants in deterministic order")
    p_extract.add_argument("-c", "--checkpoint", default=None, help="Checkpoint file path (default: isolated under .checkpoints/)")
    p_extract.add_argument("-b", "--batch-size", type=int, default=10, help="Participants per batch call (default: 10)")
    p_extract.add_argument("--fresh", action="store_true", help="Ignore existing checkpoint and start fresh")

    # Command: check
    p_check = subparsers.add_parser("check", help="Validate output without API calls")
    p_check.add_argument("-i", "--input", default=None, help="Input dataset directory")
    p_check.add_argument("-o", "--output", default=None, help="Output file path (default: OUTPUT_PATH or output.json)")

    # Command: evaluate
    p_eval = subparsers.add_parser("evaluate", help="Score output against answer key")
    p_eval.add_argument("-i", "--input", default=None, help="Input dataset directory")
    p_eval.add_argument("-o", "--output", default=None, help="Output file path (default: OUTPUT_PATH or output.json)")

    # Command: run
    p_run = subparsers.add_parser("run", help="Execute complete pipeline end-to-end")
    p_run.add_argument("input_pos", nargs="?", default=None, help="Input dataset directory (optional positional)")
    p_run.add_argument("output_pos", nargs="?", default=None, help="Output file path (optional positional)")
    p_run.add_argument("-i", "--input", default=None, help="Input dataset directory")
    p_run.add_argument("-o", "--output", default=None, help="Output file path")
    p_run.add_argument("-l", "--limit", type=int, default=None, help="Extract only the first N participants in deterministic order")
    p_run.add_argument("-b", "--batch-size", type=int, default=10, help="Participants per batch call (default: 10)")
    p_run.add_argument("--fresh", action="store_true", help="Ignore existing checkpoint and start fresh")

    return parser


def main():
    # Backward compatibility with run.sh: `python solution.py <input_dir> <output.json>`
    if (
        len(sys.argv) == 3
        and not sys.argv[1].startswith("-")
        and not sys.argv[2].startswith("-")
        and sys.argv[1] not in ("extract", "check", "evaluate", "run")
    ):
        try:
            sys.exit(run_pipeline(sys.argv[1], sys.argv[2]))
        except (FileNotFoundError, ValueError, RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)

    parser = build_cli()
    args = parser.parse_args()

    try:
        if not args.command or args.command == "run":
            in_dir = _first_not_none(
                getattr(args, "input", None),
                getattr(args, "input_pos", None),
                args.root_input,
            )
            out_file = _first_not_none(
                getattr(args, "output", None),
                getattr(args, "output_pos", None),
                args.root_output,
            )
            sys.exit(run_pipeline(
                input_dir=in_dir,
                output_path=out_file,
                limit=getattr(args, "limit", None),
                batch_size=getattr(args, "batch_size", 10),
                use_checkpoint=not getattr(args, "fresh", False),
            ))

        if args.command == "extract":
            extract_and_consolidate(
                input_dir=_first_not_none(args.input, args.root_input),
                output_path=_first_not_none(args.output, args.root_output),
                participant=args.participant,
                limit=args.limit,
                checkpoint_file=args.checkpoint,
                batch_size=args.batch_size,
                use_checkpoint=not args.fresh,
            )
        elif args.command == "check":
            valid = validate_output(
                output_path=_first_not_none(args.output, args.root_output),
                input_dir=_first_not_none(args.input, args.root_input),
            )
            sys.exit(0 if valid else 1)
        elif args.command == "evaluate":
            sys.exit(evaluate_output(
                output_path=_first_not_none(args.output, args.root_output),
                input_dir=_first_not_none(args.input, args.root_input),
            ))
    except (FileNotFoundError, ValueError, RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
