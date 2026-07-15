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
        # Hard era bounds (applied as an AND filter on candidates); null when the
        # request has no time period.
        "year_min": {"type": ["integer", "null"]},
        "year_max": {"type": ["integer", "null"]},
    },
    "required": ["name", "match", "rules", "limit", "year_min", "year_max"],
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
        "Genres MUST come from the controlled vocabulary. Prefer match=any with a few genre rules "
        "for mood-style requests. Set limit to the number of tracks the user asks for, else 30. "
        "Rule values are strings ('1980' for a decade, '1985' for a year).\n"
        "If (and only if) the request implies a time period or era, set year_min/year_max to bound it — "
        "these are a HARD filter, so use them for era requests instead of year rules. Guides: "
        "'hippie'/'Woodstock'/'flower power' ≈ 1965–1975; '60s' = 1960–1969; '70s' = 1970–1979; "
        "'80s' = 1980–1989; 'oldies' ≈ 1955–1969; 'classic rock' ≈ 1965–1985. "
        "Set BOTH year_min and year_max to null when the request has no time period.\n\n"
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
    for k in ("year_min", "year_max"):
        v = intent.get(k)
        try:
            intent[k] = int(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            intent[k] = None
    return intent


# ── Completionist enumeration (for "the entire set of X" requests) ──────────

_ENUM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "songs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["artist", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "songs"],
    "additionalProperties": False,
}


def enumerate_set(prompt: str, max_items: int = 400) -> dict:
    """List every song in the named set the prompt describes, from world knowledge,
    as ``{artist, title}`` — so the caller can match them against the library. This
    is the completionist path (e.g. 'all James Bond theme songs'): unlike the vibe
    curator it does NOT filter the library first, so cross-genre members aren't lost.
    Returns ``{name, songs:[{artist,title}]}``."""
    system = (
        "You are a music expert. The user wants the COMPLETE set of songs matching their "
        "request — e.g. 'all James Bond theme songs', 'every Beatles UK #1 single', 'the whole "
        "Now That's What I Call Music Vol. 1 tracklist'. List EVERY song that genuinely belongs "
        "to that set, from your knowledge, as {artist, title}. Be exhaustive — include all "
        "members across every era/entry/volume, not just the famous ones. Use the best-known "
        "performing artist and the canonical song title (no parenthetical notes). Do NOT invent "
        "songs and do NOT pad with loosely-related tracks — only real members of the set. Also "
        f"give the set a short descriptive name. Return at most {max_items} songs."
    )
    return _structured(_client(), system, prompt, _ENUM_SCHEMA, max_tokens=4096)


# ── Artist genre resolution (augments MusicBrainz in the genre-review step) ──

_ARTIST_GENRES_SCHEMA = {
    "type": "object",
    "properties": {
        "artists": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "genres": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "genres"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["artists"],
    "additionalProperties": False,
}


def genres_for_artists(artists: List[str], controlled_genres: List[str],
                       chunk: int = 80) -> dict:
    """Ask Claude for each artist's primary genre(s), constrained to the controlled
    vocabulary. Returns {artist_name: [genres]} (genres may be empty when genuinely
    unknown). Batched — one call per `chunk` artists — since a genre-review can leave
    many unresolved. The caller still normalizes the result against the vocabulary."""
    names = [a for a in (artists or []) if a and a.strip()]
    if not names:
        return {}
    vocab = ", ".join(controlled_genres)
    system = (
        "You are a music expert. For each artist, give their PRIMARY genre(s) — the "
        "one or two that best classify their catalog overall. Choose ONLY from this "
        "controlled vocabulary; use the exact spelling; never invent a genre:\n"
        f"{vocab}\n\n"
        "Return 1–2 genres per artist. Do NOT use 'Holiday' as an artist's genre even "
        "for artists known for Christmas songs — give their non-holiday primary genre. "
        "If you genuinely do not recognize an artist, return an empty genres array for "
        "them rather than guessing."
    )
    client = _client()
    out: dict = {}
    for i in range(0, len(names), max(1, chunk)):
        batch = names[i:i + max(1, chunk)]
        user = "Artists:\n" + "\n".join(f"- {n}" for n in batch)
        try:
            data = _structured(client, system, user, _ARTIST_GENRES_SCHEMA, max_tokens=4096)
        except Exception:
            continue
        for row in data.get("artists", []):
            nm = (row.get("name") or "").strip()
            gs = [g for g in (row.get("genres") or []) if isinstance(g, str)]
            if nm:
                out[nm] = gs
    return out


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
        "You are curating a playlist from a candidate list for the user's request. "
        "Include ONLY tracks that genuinely fit what the request asks for — its mood, era, style, or theme. "
        f"Return AT MOST {target} tracks, but quality over quantity: {target} is a ceiling, NOT a goal. "
        "It is much better to return a short list of true fits than to pad the list to reach a number. "
        "If only a handful of candidates genuinely fit, return only those few. Never include a track just to "
        "fill the count, and never include one you are unsure about — when in doubt, leave it out. "
        "The candidate list is in arbitrary order; do NOT let its ordering influence your choices. "
        "Order the chosen tracks for good flow (best first), and spread them across many different artists — "
        "avoid clustering; use at most about two tracks per artist unless the request is specifically about one artist. "
        "Return only the indices of the tracks you choose, in order."
    )
    user = f"Request: {prompt}\n\nCandidates:\n" + "\n".join(lines)
    out = _structured(_client(), system, user, _RERANK_SCHEMA, max_tokens=2048)
    seen, ordered = set(), []
    for i in out.get("indices", []):
        if isinstance(i, int) and 0 <= i < len(candidates) and i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered
