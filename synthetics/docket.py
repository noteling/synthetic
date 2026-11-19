"""Docket P1a: what this home is waiting on from one person (default: the home's default human), and since when.

Read-only and home-local (Codex ruling Q3): it reads only this home's own questions, outbox delivery states,
works and the public company roster (for aliases). It never opens another home's database and never reads
private views (C5). The JSON fields below are a stable contract (v1); add fields, never rename them.

Age (C1): many manager questions to David go through a channel that never acks (codex-desktop). Each item shows
both the age since the ask and, when a delivery ack exists, the age since delivery. Without an ack the age is
taken from the ask and the item is marked "not acked (route ...)". An item is never dropped for lacking an ack.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import capture, roster

VERSION = 1
SUMMARY_CHARS = 60  # Q5: first line of my own question, at most 60 characters


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _minutes(now, ts):
    at = _parse(ts)
    return None if at is None else max(0, int((now - at).total_seconds() // 60))


def summary(body):
    """Q5: the first non-empty line of the question body, at most SUMMARY_CHARS characters."""
    line = next((part.strip() for part in (body or "").splitlines() if part.strip()), "")
    return line if len(line) <= SUMMARY_CHARS else line[:SUMMARY_CHARS - 1].rstrip() + "…"


def delivered_at(message):
    """Earliest recorded delivery ack of an outbox message (M25 channel state or legacy M22 ack), else None."""
    times = [state.get("updated_at") for state in (message.get("channel_states") or {}).values()
             if state.get("state") == "delivered" and state.get("updated_at")]
    times += [item.get("at") for item in message.get("deliveries") or []
              if item.get("status") in ("delivered", "reconciled:delivered") and item.get("at")]
    return min(times) if times else None


def addresses(store, person=None):
    """(person ID, sorted addresses that count as that person). Person defaults to humans.json default_human.

    Addresses: the person's ID, the name and aliases from the company roster, and for the default human the
    humans.json sender IDs. Matching folds case and whitespace like roster aliases."""
    cfg = capture.humans(store)
    try:
        state = roster.company_state_for_home(store.home)
    except Exception:  # noqa: BLE001 - no or unreadable company log: match the given name only
        state = roster.company_fold([])
    name = person if person is not None else cfg.get("default_human")
    if not name:
        raise ValueError("No person given and this home's humans.json has no default_human; pass person.")
    hit = roster.resolve(state, name)
    pid = hit["id"] or name
    found = {pid, name}
    profile = state.get("people", {}).get(pid) or {}
    if profile.get("name"):
        found.add(profile["name"])
    found.update(profile.get("aliases") or [])
    if pid == cfg.get("default_human"):
        found.update(cfg.get("senders") or [])
    return pid, sorted(found)


def _age(minutes):
    if minutes is None:
        return "?"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, mins)
    return "%dm" % mins


def _short(ts):
    at = _parse(ts)
    return at.strftime("%m-%d %H:%MZ") if at else "?"


def build(store, person=None, now=None):
    now = now or datetime.now(timezone.utc)
    pid, names = addresses(store, person)
    folded = {roster.fold_alias(item) for item in names}
    messages = {m["id"]: m for m in store._all("messages")}
    stuck = {item["id"] for item in store.outbox_stuck_from(list(messages.values()), store._all("channels"),
                                                            now)["items_all"]}
    waiting = store._waiting_links()
    works = {w["id"]: w for w in store._all("works")}
    items = []
    for question in store.question_list("open"):
        targets = {question.get("to"), question.get("to_id")} - {None}
        if not any(roster.fold_alias(t) in folded for t in targets):
            continue
        message = messages.get(question.get("message_id") or "") or {}
        acked = delivered_at(message)
        routes = store.routed_by(question["to"])
        if acked:
            ack = "delivered"
        elif routes is None:
            ack = "not acked (route unknown: no channels in this home)"
        elif not routes:
            ack = "not acked (no route)"
        else:
            ack = "not acked (route %s)" % ", ".join(routes)
        asked_age = _minutes(now, question.get("created_at"))
        delivered_age = _minutes(now, acked) if acked else None
        linked = [w["id"] for w, ids in waiting if question["id"] in ids]
        items.append(dict(
            question_id=question["id"], summary=summary(question.get("body")), to=question.get("to"),
            to_id=question.get("to_id"), asked_at=question.get("created_at"), asked_age_minutes=asked_age,
            delivery=store.delivery_summary(message) if message else "unknown", delivered_at=acked,
            delivered_age_minutes=delivered_age, age_from="delivered" if acked else "asked",
            age_minutes=delivered_age if acked else asked_age, ack=ack, routes=routes,
            stuck=message.get("id") in stuck, origin_work_id=question.get("work_id"),
            works=[dict(id=wid, title=works[wid].get("title", ""), status=works[wid]["status"],
                        published=works[wid].get("publish") or None) for wid in sorted(linked)],
            # P1b (additive fields): what this question carries onward, and its hop as last reported to me.
            carries=[dict(question_id=link["question_id"], asker=link["asker"],
                          link_auth=(link.get("link_auth") or {}).get("channel")) for link in question.get("relays") or []],
            hop=store.hop_label(question), hop_since=question.get("hop_since"),
            # Docket links (additive): the onward question the hop went via, so Lookout can link it.
            hop_via=question.get("hop_via")))
    items.sort(key=lambda item: (item["asked_at"] or "", item["question_id"]))
    return dict(v=VERSION, person=pid, addresses=names, home=store.identity()["id"],
                generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), count=len(items), items=items,
                relay_owed=store.relay_owed(now))


def render(view):
    """Paste-ready daily-sync block."""
    lines = ["Waiting on %s — %s, %d open" % (view["person"], _short(view["generated_at"]), view["count"])]
    for number, item in enumerate(view["items"], 1):
        lines.append("%d. %s  %s" % (number, item["question_id"], item["summary"]))
        when = "   asked %s (%s)" % (_short(item["asked_at"]), _age(item["asked_age_minutes"]))
        if item["delivered_at"]:
            when += "  delivered %s (%s)" % (_short(item["delivered_at"]), _age(item["delivered_age_minutes"]))
        else:
            when += "  " + item["ack"]
        if item["stuck"]:
            when += "  [outbox stuck: %s]" % item["delivery"]
        lines.append(when)
        if item["hop"]:
            lines.append("   now: %s since %s" % (item["hop"], _short(item["hop_since"])))
        if item["works"]:
            lines.append("   blocks: " + "; ".join("%s %s" % (w["id"], w["title"]) for w in item["works"]))
        else:
            lines.append("   blocks: no waiting work (origin %s)" % (item["origin_work_id"] or "none"))
        for link in item["carries"]:
            lines.append("   carries: %s from %s%s" % (link["question_id"], link["asker"],
                                                      "" if link["link_auth"] else " (link auth null)"))
    if not view["items"]:
        lines.append("(nothing open)")
    owed = view.get("relay_owed") or []
    if owed:
        lines.append("Relay owed (answered, not yet passed on):")
        for item in owed:
            lines.append("- %s to %s, answered %s (%s) via %s" % (
                item["question_id"], item["asker"], _short(item["answered_at"]), _age(item["age_minutes"]),
                item["via"]))
    return "\n".join(lines)
