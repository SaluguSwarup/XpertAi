"""Participant-centred pre-processing and post-processing extraction layer.

This module is strictly isolated: it handles context collection, prompt building,
and defensive schema validation. It does NOT make network or LLM calls by default.
"""
import json
import re
from pathlib import Path

# Exact schema enum sets from schema.json
CIRCLES = {
    "spouse", "child", "parent", "in-law", "sibling", "relative",
    "friend", "colleague", "client", "vendor", "partner", "staff"
}
ORG_TYPES = {"company", "residence", "vacation home", "workspace", "society", "school", "other"}
ORG_ROLES = {"owner", "partner", "helper", "resident", "parent", "member", "other"}
AREAS = {"self", "spouse", "kids", "parents", "work", "friends", "experiences", "finances", "health", "home"}
GOAL_TYPES = {"habit", "milestone", "experience"}
HORIZONS = {"3mo", "1yr", "3yr", "5yr"}
LOOP_TYPES = {"commitment", "followup", "deadline", "question"}
STATUSES = {"open", "done", "dropped"}
ACTIONS = {"assist", "remind", "leave"}


def build_participant_context(person_id, chats_by_participant_id, messages_by_chat_id):
    """
    Collect all relevant chats and messages for one participant.
    Preserves exact source IDs, metadata, and chronological message order.
    """
    chats = chats_by_participant_id.get(person_id, [])
    context = {
        "focus_participant_id": person_id,
        "chats": []
    }
    for c in chats:
        cid = c["chat_id"]
        chat_entry = {
            "chat_id": cid,
            "name": c["name"],
            "is_group": c["is_group"],
            "participants": c["participants"],
            "messages": []
        }
        for m in messages_by_chat_id.get(cid, []):
            chat_entry["messages"].append({
                "id": m["id"],
                "ts": m.get("ts"),
                "sender_id": m.get("sender_id"),
                "sender_name": m.get("sender_name", ""),
                "kind": m.get("kind", "text"),
                "body": m.get("body", ""),
                "quoted_id": m.get("quoted_id")
            })
        context["chats"].append(chat_entry)
    return context


def create_extraction_prompt(context, seeded_circles=None):
    """
    Build the extraction prompt using the participant context and permitted seed info.
    Requests extraction across the five categories defined in schema.json.
    """
    pid = context["focus_participant_id"]
    seeded_info = ""
    if seeded_circles and pid in seeded_circles:
        seeded_info = f"\nNote: {pid} is already known to belong to the '{seeded_circles[pid]}' circle. Do not contradict this."

    system_prompt = f"""You are the life-understanding engine for Xpert, an assistant working through WhatsApp.
Arjun is the user and is designated with sender_id "ME".
You are analyzing WhatsApp conversations involving contact {pid}.{seeded_info}

Extract information across the 5 output categories defined strictly below. Ground every single extraction in the provided message evidence. Do NOT invent people, organisations, goals, tasks, deadlines, or relationships.

1. PEOPLE:
   - Identify relationship circles relative to Arjun ("ME") for {pid} and other contacts in these chats.
   - Never include "ME" in the people list.
   - "circle" MUST be one of: {sorted(list(CIRCLES))}
   - Optional "links": [{{"organisation": "<org_id>", "link": "runs"|"supports"}}]
   - Optional "looks_after": ["<person_id>"] (e.g. driver looking after kids)

2. ORGANISATIONS:
   - Identify companies, residences, clubs, or entities discussed.
   - "id": a short slug identifier (e.g. "home", "brightside")
   - "name": full entity name
   - "type": "company"|"residence"|"vacation home"|"workspace"|"society"|"school"|"other"
   - "side": "home"|"work"
   - "role": "owner"|"partner"|"helper"|"resident"|"parent"|"member"|"other"

3. CHATS:
   - Determine whether each chat in the context belongs to a person or an organisation.
   - Give exactly one of: {{"chat_id": "<cid>", "person": "<person_id>"}} OR {{"chat_id": "<cid>", "organisation": "<org_id>"}}

4. GOALS:
   - Ambitions, habits, or milestones explicitly discussed for Arjun. Maximum 10 items.
   - "title": clear goal description
   - "area": one of {sorted(list(AREAS))}
   - "type": "habit"|"milestone"|"experience"
   - "horizon": "3mo"|"1yr"|"3yr"|"5yr"
   - "evidence": non-empty list of message IDs

5. OPEN_LOOPS:
   - Commitments, follow-ups, deadlines, or open questions.
   - "title": clear task description
   - "type": "commitment"|"followup"|"deadline"|"question"
   - "owner": "ME" or person ID who owes it
   - "chat_id": chat ID where it occurs
   - "source_msg": message ID where the loop originated
   - "due": "YYYY-MM-DD" or null
   - "status": "open"|"done"|"dropped"
     CONSERVATIVE RULE: Do not mark a loop as "done" or "dropped" unless subsequent messages provide unambiguous evidence that the task was completed or explicitly cancelled. Ambiguous statements or counterparty remarks do not prove cancellation. When in doubt, mark "open".
   - "closed_by": message ID showing explicit closure, or null if open
   - "help": ONLY if status == "open" (MUST be null if status is "done" or "dropped"):
     - "action": "assist"|"remind"|"leave"
     - If assist: "next_step" (string), "needs_yes" (bool)
     - If remind: "remind_at" (ISO date-time string e.g. "2026-10-25T10:00:00+05:30")
     - If leave: "reason" (string explaining why Xpert should stay out)

Return ONLY a valid JSON object with keys: "people", "organisations", "chats", "goals", "open_loops"."""

    user_prompt = f"Here is the conversation context for participant {pid}:\n\n" + json.dumps(context, indent=2, ensure_ascii=False)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]


def is_unambiguous_closure(cls_msg, src_msg, owner):
    """
    Check if a closing message provides unambiguous, explicit proof of closure.
    Counterparty remarks (e.g. 'Never mind the plan, coach gave me one') or casual banter
    do not constitute explicit resolution of commitments.
    """
    if not cls_msg:
        return False
    body = cls_msg.get("body", "").strip().lower()
    sender = cls_msg.get("sender_id")

    # Closure must come from either the task owner or Arjun ("ME")
    if sender not in (owner, "ME"):
        return False

    # If the task is owed by 'ME', but the message is from someone else,
    # counterparty comments like 'never mind' do not prove Arjun resolved or cancelled his promise.
    if owner == "ME" and sender != "ME":
        return False

    # Check for direct explicit task completion keywords
    explicit_done_markers = [
        "done", "booked", "paid", "transferred", "submitted", "shared",
        "attached", "completed", "resolved", "approved", "confirmed"
    ]
    return any(re.search(rf"\b{marker}\b", body) for marker in explicit_done_markers)


def _extract_json_object(raw_text):
    """Parse JSON from direct output, markdown code fences, or embedded JSON objects."""
    text = (raw_text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)

    return json.loads(text)


def parse_and_validate_extraction(raw_text, valid_chat_ids, valid_person_ids, valid_msg_ids,
                                  messages_by_id=None, require_explicit_closure=True):
    """
    Parse supplied JSON string and validate structure, enums, referenced IDs,
    and enforce conservative open-loop closure rules.

    Returns:
        (validated_data, errors, flags)
        - validated_data: dict with cleaned schema-compliant entries
        - errors: list of validation error strings (e.g. invalid IDs, malformed shapes)
        - flags: list of review flags (e.g. conservative review warnings)
    """
    try:
        data = _extract_json_object(raw_text)
    except json.JSONDecodeError as e:
        return None, [f"JSONDecodeError: {e}"], []

    errors = []
    flags = []

    if not isinstance(data, dict):
        return None, ["Root output is not a JSON object."], []

    # Verify top-level structure
    for key in ("people", "organisations", "chats", "goals", "open_loops"):
        if key not in data or not isinstance(data[key], list):
            data[key] = []

    # 1. Validate People
    clean_people = []
    seen_people = set()
    for i, p in enumerate(data.get("people", [])):
        loc = f"people[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{loc}: must be an object.")
            continue
        pid = p.get("id") or p.get("person_id")
        if pid == "ME":
            errors.append(f"{loc}: 'ME' must never be included in people.")
            continue
        if pid not in valid_person_ids:
            errors.append(f"{loc}: unknown person ID '{pid}'.")
            continue
        if pid in seen_people:
            errors.append(f"{loc}: duplicate person ID '{pid}'.")
            continue
        seen_people.add(pid)

        circle = p.get("circle")
        if circle not in CIRCLES:
            errors.append(f"{loc}: circle '{circle}' is not allowed in schema.json.")
            continue

        clean_p = {"id": pid, "circle": circle}
        if "links" in p and isinstance(p["links"], list):
            clean_links = []
            for link_obj in p["links"]:
                if isinstance(link_obj, dict) and link_obj.get("organisation") and link_obj.get("link") in ("runs", "supports"):
                    clean_links.append({"organisation": link_obj["organisation"], "link": link_obj["link"]})
            if clean_links:
                clean_p["links"] = clean_links
        if "looks_after" in p and isinstance(p["looks_after"], list):
            valid_la = [other for other in p["looks_after"] if other in valid_person_ids]
            if valid_la:
                clean_p["looks_after"] = valid_la
        clean_people.append(clean_p)
    data["people"] = clean_people

    # 2. Validate Organisations
    clean_orgs = []
    org_ids = set()
    for i, o in enumerate(data.get("organisations", [])):
        loc = f"organisations[{i}]"
        if not isinstance(o, dict) or not o.get("id"):
            errors.append(f"{loc}: missing or empty 'id'.")
            continue
        oid = str(o["id"])
        if oid in org_ids:
            errors.append(f"{loc}: duplicate organisation ID '{oid}'.")
            continue
        org_ids.add(oid)

        clean_o = {
            "id": oid,
            "name": str(o.get("name", oid)),
            "type": o.get("type") if o.get("type") in ORG_TYPES else "other",
            "side": o.get("side") if o.get("side") in ("home", "work") else "work",
            "role": o.get("role") if o.get("role") in ORG_ROLES else "member"
        }
        clean_orgs.append(clean_o)
    data["organisations"] = clean_orgs

    # 3. Validate Chats
    clean_chats = []
    for i, c in enumerate(data.get("chats", [])):
        loc = f"chats[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{loc}: must be an object.")
            continue
        cid = c.get("chat_id")
        if cid not in valid_chat_ids:
            errors.append(f"{loc}: unknown chat_id '{cid}'.")
            continue
        has_p = "person" in c and c["person"] is not None
        has_o = "organisation" in c and c["organisation"] is not None
        if has_p == has_o:
            errors.append(f"{loc}: must contain exactly one of 'person' or 'organisation'.")
            continue
        if has_p:
            if c["person"] not in valid_person_ids:
                errors.append(f"{loc}: person '{c['person']}' is not a valid participant.")
                continue
            clean_chats.append({"chat_id": cid, "person": c["person"]})
        else:
            clean_chats.append({"chat_id": cid, "organisation": str(c["organisation"])})
    data["chats"] = clean_chats

    # 4. Validate Goals
    clean_goals = []
    for i, g in enumerate(data.get("goals", [])):
        loc = f"goals[{i}]"
        if not isinstance(g, dict) or not g.get("title"):
            errors.append(f"{loc}: missing title.")
            continue
        area = g.get("area")
        gtype = g.get("type")
        horizon = g.get("horizon")
        if area not in AREAS:
            errors.append(f"{loc}: invalid area '{area}'.")
            continue
        if gtype not in GOAL_TYPES:
            errors.append(f"{loc}: invalid goal type '{gtype}'.")
            continue
        if horizon not in HORIZONS:
            errors.append(f"{loc}: invalid horizon '{horizon}'.")
            continue

        raw_ev = g.get("evidence", [])
        valid_ev = [m for m in raw_ev if m in valid_msg_ids]
        if not valid_ev:
            errors.append(f"{loc}: no valid evidence message IDs found in {raw_ev}.")
            continue

        clean_goals.append({
            "title": str(g["title"]),
            "area": area,
            "type": gtype,
            "horizon": horizon,
            "evidence": valid_ev
        })
    data["goals"] = clean_goals[:10]

    # 5. Validate Open Loops (with Conservative Closure Policy)
    clean_loops = []
    for i, l in enumerate(data.get("open_loops", [])):
        loc = f"open_loops[{i}]"
        if not isinstance(l, dict) or not l.get("title"):
            errors.append(f"{loc}: missing title.")
            continue

        title = str(l["title"])
        ltype = l.get("type")
        if ltype not in LOOP_TYPES:
            errors.append(f"{loc} ('{title}'): invalid loop type '{ltype}'.")
            continue

        owner = l.get("owner")
        if owner != "ME" and owner not in valid_person_ids:
            errors.append(f"{loc} ('{title}'): invalid owner '{owner}'.")
            continue

        cid = l.get("chat_id")
        if cid not in valid_chat_ids:
            errors.append(f"{loc} ('{title}'): unknown chat_id '{cid}'.")
            continue

        src = l.get("source_msg")
        if src not in valid_msg_ids:
            errors.append(f"{loc} ('{title}'): source_msg '{src}' does not exist.")
            continue

        status = l.get("status")
        if status not in STATUSES:
            errors.append(f"{loc} ('{title}'): invalid status '{status}'.")
            status = "open"

        closed_by = l.get("closed_by")

        # Conservative closure verification
        if status in ("done", "dropped"):
            if not closed_by:
                err_msg = f"{loc} ('{title}'): marked '{status}' but 'closed_by' message ID is missing. Unproven closures are rejected."
                errors.append(err_msg)
                if require_explicit_closure:
                    status = "open"
                    closed_by = None
            elif closed_by not in valid_msg_ids:
                errors.append(f"{loc} ('{title}'): 'closed_by' message '{closed_by}' does not exist in input.")
                if require_explicit_closure:
                    status = "open"
                    closed_by = None
            else:
                # closed_by is a valid message ID in the input
                src_m = messages_by_id.get(src) if messages_by_id else None
                cls_m = messages_by_id.get(closed_by) if messages_by_id else None

                if src_m and cls_m and cls_m.get("ts", 0) < src_m.get("ts", 0):
                    errors.append(f"{loc} ('{title}'): 'closed_by' {closed_by} occurred BEFORE 'source_msg' {src}.")
                    if require_explicit_closure:
                        status = "open"
                        closed_by = None
                elif require_explicit_closure:
                    # Do not assume valid message ID or later timestamp proves closure:
                    # Require explicit, unambiguous closure evidence in the cited message.
                    if not (cls_m and is_unambiguous_closure(cls_m, src_m, owner)):
                        cls_text = cls_m.get("body", "").strip() if cls_m else ""
                        flags.append(
                            f"{loc} ('{title}'): closure by message {closed_by} ('{cls_text}') is not clearly supported. "
                            f"Reverted to 'open' with conservative help policy to prevent false-closure penalty."
                        )
                        status = "open"
                        closed_by = None
                    else:
                        cls_text = cls_m.get("body", "").strip()
                        flags.append(
                            f"{loc} ('{title}'): status is '{status}' via message {closed_by} ('{cls_text}'). "
                            f"Verified as explicitly supported."
                        )

        # Enforce schema constraint on help
        help_entry = None
        if status == "open":
            raw_help = l.get("help")
            if isinstance(raw_help, dict) and raw_help.get("action") in ACTIONS:
                help_entry = {"action": raw_help["action"]}
                if raw_help["action"] == "assist":
                    if "next_step" in raw_help:
                        help_entry["next_step"] = str(raw_help["next_step"])
                    if "needs_yes" in raw_help:
                        help_entry["needs_yes"] = bool(raw_help["needs_yes"])
                elif raw_help["action"] == "remind" and "remind_at" in raw_help:
                    help_entry["remind_at"] = str(raw_help["remind_at"])
                elif raw_help["action"] == "leave" and "reason" in raw_help:
                    help_entry["reason"] = str(raw_help["reason"])
            else:
                # Default conservative help policy for open loops
                help_entry = {
                    "action": "leave",
                    "reason": "Default conservative policy: closure not explicitly proven"
                }
        else:
            if l.get("help") is not None:
                errors.append(f"{loc} ('{title}'): 'help' must be null for status '{status}'. Stripped.")
            help_entry = None

        due = l.get("due")
        if due is not None and not (isinstance(due, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", due)):
            due = None

        clean_loops.append({
            "title": title,
            "type": ltype,
            "owner": owner,
            "chat_id": cid,
            "source_msg": src,
            "due": due,
            "status": status,
            "closed_by": closed_by if status in ("done", "dropped") else None,
            "help": help_entry
        })
    data["open_loops"] = clean_loops

    return data, errors, flags


def build_batch_context(participant_ids, chats_by_participant_id, messages_by_chat_id):
    """
    Collect all unique chats and chronological messages for a batch of participants.
    Preserves exact source IDs, metadata, and chronological message order.
    """
    unique_chats = {}
    for pid in participant_ids:
        for c in chats_by_participant_id.get(pid, []):
            cid = c["chat_id"]
            if cid not in unique_chats:
                unique_chats[cid] = {
                    "chat_id": cid,
                    "name": c["name"],
                    "is_group": c["is_group"],
                    "participants": c["participants"],
                    "messages": []
                }
                for m in messages_by_chat_id.get(cid, []):
                    unique_chats[cid]["messages"].append({
                        "id": m["id"],
                        "ts": m.get("ts"),
                        "sender_id": m.get("sender_id"),
                        "sender_name": m.get("sender_name", ""),
                        "kind": m.get("kind", "text"),
                        "body": m.get("body", ""),
                        "quoted_id": m.get("quoted_id")
                    })

    return {
        "focus_participants": participant_ids,
        "chats": list(unique_chats.values())
    }


def create_batch_prompt(batch_context):
    """
    Build extraction prompt for multiple focus participants in one LLM request.
    """
    pids = batch_context["focus_participants"]
    circles_list = sorted(list(CIRCLES))
    areas_list = sorted(list(AREAS))

    system_prompt = f"""You are the life-understanding engine for Xpert, an assistant working through WhatsApp.
Arjun is the user and is designated with sender_id "ME".
You are analyzing WhatsApp conversations involving the following focus contacts: {json.dumps(pids)}.

Extract information across the 5 output categories defined strictly below. Ground every single extraction in the provided message evidence. Do NOT invent people, organisations, goals, tasks, deadlines, or relationships.

1. PEOPLE:
   - Identify relationship circles relative to Arjun ("ME") for each of the focus contacts: {json.dumps(pids)}, and other key contacts in these chats.
   - For EACH focus contact in {json.dumps(pids)}, you MUST determine their relationship circle to Arjun ("ME").
   - Never include "ME" in the people list.
   - "circle" MUST be one of: {circles_list}
   - Optional "links": [{{"organisation": "<org_id>", "link": "runs"|"supports"}}]
   - Optional "looks_after": ["<person_id>"]

2. ORGANISATIONS:
   - Identify companies, residences, clubs, or entities discussed.
   - "id": a short slug identifier (e.g. "home", "brightside")
   - "name": full entity name
   - "type": "company"|"residence"|"vacation home"|"workspace"|"society"|"school"|"other"
   - "side": "home"|"work"
   - "role": "owner"|"partner"|"helper"|"resident"|"parent"|"member"|"other"

3. CHATS:
   - Determine whether each chat in the context belongs to a person or an organisation.
   - Give exactly one of: {{"chat_id": "<cid>", "person": "<person_id>"}} OR {{"chat_id": "<cid>", "organisation": "<org_id>"}}

4. GOALS:
   - Ambitions, habits, or milestones explicitly discussed for Arjun. Maximum 10 items.
   - "title": clear goal description
   - "area": one of {areas_list}
   - "type": "habit"|"milestone"|"experience"
   - "horizon": "3mo"|"1yr"|"3yr"|"5yr"
   - "evidence": non-empty list of message IDs

5. OPEN_LOOPS:
   - Commitments, follow-ups, deadlines, or open questions.
   - "title": clear task description
   - "type": "commitment"|"followup"|"deadline"|"question"
   - "owner": "ME" or person ID who owes it
   - "chat_id": chat ID where it occurs
   - "source_msg": message ID where the loop originated
   - "due": "YYYY-MM-DD" or null
   - "status": "open"|"done"|"dropped"
     CONSERVATIVE RULE: Do not mark a loop as "done" or "dropped" unless subsequent messages provide unambiguous evidence that the task was completed or explicitly cancelled. Ambiguous statements or counterparty remarks do not prove cancellation. When in doubt, mark "open".
   - "closed_by": message ID showing explicit closure, or null if open
   - "help": ONLY if status == "open" (MUST be null if status is "done" or "dropped"):
     - "action": "assist"|"remind"|"leave"
     - If assist: "next_step" (string), "needs_yes" (bool)
     - If remind: "remind_at" (ISO date-time string e.g. "2026-10-25T10:00:00+05:30")
     - If leave: "reason" (string explaining why Xpert should stay out)

Return ONLY a valid JSON object with keys: "people", "organisations", "chats", "goals", "open_loops"."""

    user_prompt = f"Here is the conversation context for focus participants {json.dumps(pids)}:\n\n" + json.dumps(batch_context, indent=2, ensure_ascii=False)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]


def call_llm_extractor(context, seeded_circles=None, model=None, timeout=300):
    """
    Invoke llm.chat() for either single participant context or batch context.
    """
    import os
    import llm  # Scoped import
    if "focus_participants" in context:
        messages = create_batch_prompt(context)
    else:
        messages = create_extraction_prompt(context, seeded_circles)

    selected_model = model or os.environ.get("LLM_MODEL")
    return llm.chat(
        messages=messages,
        model=selected_model,
        temperature=0,
        response_format={"type": "json_object"},
        timeout=timeout
    )
