"""AI playlist curation via Claude — two-stage (prompt → intent → re-rank).

Isolated here so the Anthropic dependency and key handling stay out of the rest
of the app. Degrades cleanly: if `ANTHROPIC_API_KEY` is unset or the `anthropic`
SDK isn't installed, `is_enabled()` returns False and the caller falls back to
smart-only playlists.

The key is read from the environment by the SDK — it is NEVER stored in code,
the DB, or a playlist spec.
"""

import json
import os
import re
from typing import List, Optional

# Handoff §4/§9: cheap curation defaults to Haiku; overridable via env.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")


def is_enabled() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _client():
    import anthropic
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # Fall back to the first {...} span if the model added prose.
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return text


def _structured(client, system: str, user: str, schema: dict, max_tokens: int) -> dict:
    """One JSON-returning call, using structured output where the SDK supports it."""
    kwargs = dict(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.messages.create(
            output_config={"format": {"type": "json_schema", "schema": schema}}, **kwargs
        )
    except TypeError:
        # Older SDK without output_config — ask for raw JSON instead.
        resp = client.messages.create(
            system=system + "\n\nRespond with ONLY valid JSON in the requested shape — no prose.",
            model=ANTHROPIC_MODEL, max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return json.loads(_extract_json(text))


# ── Stage 1: natural-language prompt → structured intent (a smart-playlist spec) ──

_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "match": {"type": "string", "enum": ["all", "any"]},
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": ["genre", "artist", "album", "year", "decade"]},
                    "op": {"type": "string", "enum": ["is", "contains", "gte", "lte"]},
                    "value": {"type": "string"},
                },
                "required": ["field", "op", "value"],
                "additionalProperties": False,
            },
        },
        "limit": {"type": "integer"},
    },
    "required": ["name", "match", "rules", "limit"],
    "additionalProperties": False,
}


def prompt_to_intent(prompt: str, facets: dict, controlled_genres: List[str]) -> dict:
    """Ask Claude to turn a request into a filter grounded in the actual library."""
    genres = ", ".join(facets.get("genres") or []) or "(none indexed)"
    artists = facets.get("artists") or []
    artist_hint = ", ".join(artists[:80])
    yr_min, yr_max = facets.get("year_min"), facets.get("year_max")

    system = (
        "You are a music librarian. Convert the user's request into a filter over THEIR library. "
        "Only reference genres, artists, and years that plausibly exist in the library described below. "
        "Genres MUST come from the controlled vocabulary. Prefer match=any with a few genre/decade rules "
        "for mood-style requests. Set limit to the number of tracks the user asks for, else 30. "
        "Values are strings ('1980' for a decade, '1985' for a year).\n\n"
        f"Controlled genres: {', '.join(controlled_genres)}\n"
        f"Genres present in the library: {genres}\n"
        f"Year range: {yr_min}–{yr_max}\n"
        f"Some artists in the library: {artist_hint}"
    )
    intent = _structured(_client(), system, prompt, _INTENT_SCHEMA, max_tokens=1024)

    # Coerce numeric fields the rule engine expects as ints.
    for r in intent.get("rules", []):
        if r.get("field") in ("year", "decade") or r.get("op") in ("gte", "lte"):
            try:
                r["value"] = int(str(r["value"]).strip())
            except (TypeError, ValueError):
                pass
    return intent


# ── Stage 2: re-rank / curate candidates against the original prompt ──

_RERANK_SCHEMA = {
    "type": "object",
    "properties": {"indices": {"type": "array", "items": {"type": "integer"}}},
    "required": ["indices"],
    "additionalProperties": False,
}


def rerank(prompt: str, candidates: List[dict], target: int) -> List[int]:
    """Return candidate indices in playlist order (best first), curated to ~target."""
    lines = []
    for i, c in enumerate(candidates):
        meta = " · ".join(x for x in [c.get("genre"), str(c.get("year")) if c.get("year") else None] if x)
        lines.append(f"{i}: {c.get('artist','')} — {c.get('title','')}" + (f"  [{meta}]" if meta else ""))
    system = (
        f"Curate a playlist of up to {target} tracks from the candidates for the user's request. "
        "Pick the best fits and order them for good flow (best first). Drop poor fits. "
        "Spread the selection across many different artists — avoid clustering; use at most about "
        "two tracks per artist unless the request is specifically about one artist. "
        "Return only the indices of chosen candidates, in order."
    )
    user = f"Request: {prompt}\n\nCandidates:\n" + "\n".join(lines)
    out = _structured(_client(), system, user, _RERANK_SCHEMA, max_tokens=2048)
    seen, ordered = set(), []
    for i in out.get("indices", []):
        if isinstance(i, int) and 0 <= i < len(candidates) and i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered
