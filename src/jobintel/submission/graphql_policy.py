"""Privacy-safe GraphQL inspection for the stop-only runner's network guard.

A GraphQL endpoint multiplexes reads and writes behind one URL, so it can never be
authorized by URL. A POST to it is allowed only when ALL of these hold:

* the JSON body is a single operation object (no batch) with a string `query` and an
  `operationName` that is a plain GraphQL name;
* the query document, parsed locally, contains exactly one operation definition, it is a
  `query` (not a mutation or subscription) and its name equals `operationName`;
* (endpoint, operationName) is in an explicit per-run read allowlist for the approved host.

Anything else -- mutation, subscription, missing/ambiguous name, persisted query without a
document, malformed JSON or GraphQL, oversized body, URL-level operation name that disagrees
-- is "unknown" or not allowed, and stays blocked. Nothing is learned or auto-authorized.

Form-value writes (Ashby autosave) are a SEPARATE, narrower permission: an exact
(endpoint, operationName) pair in a per-run form-write allowlist, which the guard honours only
for a single `mutation` and only while the runner is inside an approved field fill (see
browserbase_runner). This module just decides whether a request matches such an entry; the
fill-window check lives in the runner. A form-write name must pass the same final-action
check as a read, so submit/apply/confirm/... mutations can never be authorized.

Only the operation name (when it is a plain identifier) and the operation type ever leave
this module; variables, the query text and the body are never returned or stored.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

MAX_BODY_CHARS = 64_000
MAX_READS = 10
MAX_FORM_WRITES = 3
NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]{0,99}")
# Names suggesting a final or write action are never authorizable, even as a "query".
# Matching is identifier-aware (see is_dangerous_name): the name is split into CamelCase /
# PascalCase / snake_case / UPPER_CASE words, then checked in two fail-closed passes.
DANGEROUS_TERMS = ("submit", "apply", "send", "finish", "complete", "confirm", "done", "final",
                   "create", "update", "delete", "upload", "save", "mutat", "upsert", "remove")
# Inflections/nominalizations that do not contain a DANGEROUS_TERMS stem verbatim.
DANGEROUS_WORDS = frozenset({"sent", "applied", "applies", "submission", "submissions", "completion",
                             "creation", "deletion", "removal", "finalize", "finalise"})
# Whole words that merely embed a stem and carry no final/write meaning. A stem found inside any
# other word (e.g. "Resubmit", "Autosave", "Finalize") or spanning word boundaries stays dangerous.
# "Complete" as its own word is always dangerous: lexically, "GetCompleteProfileData" (adjective)
# cannot be told apart from "CompleteApplication" (verb), so both are refused.
BENIGN_WORDS = frozenset({"autocomplete", "saved"})
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
URL_OPERATION_KEYS = ("op", "operation", "operationname")
_PUNCT = set("!$&()[]{}:=@|")
_SKIP = set("0123456789.+-")


def name_words(name: str) -> list[str]:
    """Deterministic lexical split of a GraphQL name into lowercase words: underscores separate,
    a lowercase->uppercase step starts a word, an acronym ends before its last capital when a
    lowercase letter follows (APIAutocomplete -> api, autocomplete), digit runs stand alone."""
    return [w.casefold() for w in _WORD.findall(name)]


def is_dangerous_name(name: str) -> bool:
    """True if the name may denote a final or write action. Fail closed:
    1. any word is a dangerous stem or one of DANGEROUS_WORDS (Submit, apply, SEND, sent, ...);
    2. any stem occurring anywhere in the joined words, unless that occurrence lies wholly inside
       a single word listed in BENIGN_WORDS (so "complete" inside "Autocomplete" is allowed, but
       "sUbMiT", "SUB_MIT", "Resubmit" or "Autosave" are not)."""
    words = name_words(name)
    if not words or any(w in DANGEROUS_WORDS or w in DANGEROUS_TERMS for w in words):
        return True
    joined, spans, pos = "".join(words), [], 0
    for w in words:
        spans.append((pos, pos + len(w), w))
        pos += len(w)
    for term in DANGEROUS_TERMS:
        start = joined.find(term)
        while start >= 0:
            end = start + len(term)
            if not any(a <= start and end <= b and w in BENIGN_WORDS for a, b, w in spans):
                return True
            start = joined.find(term, start + 1)
    return False


def endpoint_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def looks_like_graphql(url: str, body: str | None) -> bool:
    if "graphql" in urlsplit(url).path.casefold():
        return True
    try:
        data = json.loads(body) if body and len(body) <= MAX_BODY_CHARS else None
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    return any(isinstance(d, dict) and ("query" in d or "operationName" in d) for d in items)


def _tokens(doc: str):
    """GraphQL lexer: names and punctuators only; strings, comments and numbers are skipped.
    Returns None for anything it does not understand (fail closed)."""
    out, i, n = [], 0, len(doc)
    while i < n:
        c = doc[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            j = doc.find("\n", i)
            i = n if j < 0 else j + 1
        elif doc.startswith('"""', i):
            j = i + 3
            while True:
                j = doc.find('"""', j)
                if j < 0:
                    return None
                if doc[j - 1] != "\\":
                    break
                j += 3
            i = j + 3
        elif c == '"':
            j = i + 1
            while j < n and doc[j] != '"':
                if doc[j] == "\n":
                    return None
                j += 2 if doc[j] == "\\" else 1
            if j >= n:
                return None
            i = j + 1
        elif doc.startswith("...", i):
            i += 3
        elif c in _PUNCT:
            out.append(c)
            i += 1
        elif c.isascii() and (c.isalpha() or c == "_"):
            m = NAME.match(doc, i)
            out.append(("name", m.group(0)))
            i = m.end()
        elif c in _SKIP:
            i += 1
        else:
            return None
    return out


def operations(doc: str):
    """[(type, name-or-None), ...] for every executable operation; None if not parseable."""
    t = _tokens(doc)
    if t is None:
        return None
    ops, pos = [], 0

    def skip_to_selection(p):
        depth = 0
        while p < len(t) and not (t[p] == "{" and depth == 0):
            depth += t[p] == "("
            depth -= t[p] == ")"
            p += 1
        return p if p < len(t) else None

    def skip_block(p):
        depth = 0
        while p < len(t):
            depth += t[p] == "{"
            depth -= t[p] == "}"
            p += 1
            if depth == 0:
                return p
        return None

    while pos < len(t):
        tok = t[pos]
        if tok == "{":
            ops.append(("query", None))  # anonymous shorthand
        elif isinstance(tok, tuple) and tok[1] in {"query", "mutation", "subscription"}:
            kind, pos = tok[1], pos + 1
            name = t[pos][1] if pos < len(t) and isinstance(t[pos], tuple) else None
            ops.append((kind, name))
            pos = skip_to_selection(pos)
        elif isinstance(tok, tuple) and tok[1] == "fragment":
            pos = skip_to_selection(pos + 1)
        else:
            return None  # schema definitions or anything unexpected
        if pos is None:
            return None
        pos = skip_block(pos)
        if pos is None:
            return None
    return ops


def inspect(url: str, body: str | None) -> dict:
    """{'graphql_operation_name': plain name or None, 'graphql_operation_type': query|mutation|
    subscription|unknown, 'single_operation': bool}. Never returns variables or query text."""
    unknown = {"graphql_operation_name": None, "graphql_operation_type": "unknown", "single_operation": False}
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        return unknown
    try:
        data = json.loads(body)
    except ValueError:
        return unknown
    if not isinstance(data, dict):
        return unknown  # batches are never inspected or allowed
    raw_name = data.get("operationName")
    name = raw_name if isinstance(raw_name, str) and NAME.fullmatch(raw_name) else None
    out = dict(unknown, graphql_operation_name=name)
    query = data.get("query")
    if name is None or not isinstance(query, str):
        return out
    params = {k.casefold(): v for k, v in parse_qs(urlsplit(url).query).items()}
    if any(v != [name] for k, v in params.items() if k in URL_OPERATION_KEYS):
        return out  # URL routing disagrees with the body
    ops = operations(query)
    if not ops:
        return out
    named = [kind for kind, op_name in ops if op_name == name]
    if len(named) == 1:
        out["graphql_operation_type"] = named[0]
        out["single_operation"] = len(ops) == 1
    return out


def _validate_entries(application_url: str, entries, *, limit: int, what: str) -> frozenset:
    host = urlsplit(application_url).hostname
    entries = tuple((str(e), str(n)) for e, n in entries)
    if len(entries) > limit or len(set(entries)) != len(entries):
        raise ValueError(f"A GraphQL {what} allowlist holds at most {limit} distinct exact entries")
    for endpoint, name in entries:
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or parts.hostname != host or parts.username or parts.password
                or parts.query or parts.fragment or "*" in endpoint or not parts.path):
            raise ValueError(f"GraphQL {what} endpoint must be an exact https URL on the approved host")
        if not NAME.fullmatch(name) or is_dangerous_name(name):
            raise ValueError(f"GraphQL {what} must name one exact, non-final operation")
    return frozenset(entries)


def validate_reads(application_url: str, entries) -> frozenset:
    """Explicit per-run (endpoint URL, operationName) read authorizations. Endpoint: https on the
    approved host, exact path, no query/fragment/credentials/wildcards. Name: a plain GraphQL name
    with no final/write-action meaning. A read entry never authorizes a mutation."""
    return _validate_entries(application_url, entries, limit=MAX_READS, what="read")


def validate_form_writes(application_url: str, entries) -> frozenset:
    """Explicit per-run (endpoint URL, operationName) form-value mutation authorizations, e.g.
    Ashby's ApiSetFormValue autosave. Same exact-endpoint and final-action rules as reads. An entry
    only makes the mutation ELIGIBLE: the runner allows it solely inside an approved field fill."""
    return _validate_entries(application_url, entries, limit=MAX_FORM_WRITES, what="form-write")


def is_allowed_read(method: str, resource_type: str, url: str, info: dict, reads: frozenset) -> bool:
    name = info["graphql_operation_name"]
    return (method == "POST" and resource_type in {"xhr", "fetch"} and info["graphql_operation_type"] == "query"
            and info["single_operation"] and name is not None
            and not is_dangerous_name(name)
            and (endpoint_of(url), name) in reads)


def is_allowed_form_write(method: str, resource_type: str, url: str, info: dict, form_writes: frozenset) -> bool:
    """The request matches an explicit form-write entry: one named mutation, exact endpoint and name.
    Callers must ALSO require an open approved-field-fill window; this alone authorizes nothing."""
    name = info["graphql_operation_name"]
    return (method == "POST" and resource_type in {"xhr", "fetch"} and info["graphql_operation_type"] == "mutation"
            and info["single_operation"] and name is not None
            and not is_dangerous_name(name)
            and (endpoint_of(url), name) in form_writes)
