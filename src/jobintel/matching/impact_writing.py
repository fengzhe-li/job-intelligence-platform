"""CV Impact & Ownership Writing (policy CV_IMPACT_OWNERSHIP_V1) and Engineering Value Translation.

Sits between evidence selection and claim grounding / page fitting:

    existing evidence selection
      -> Impact & Ownership Bullet Composition   (compose_bullet / compose_project)
      -> Engineering Value Translation            (translate_value)
      -> Claim Grounding / Overclaim Review       (caller's grounding check + review_overclaims)
      -> Writing Quality Gate                     (writing_quality_report)
      -> one-page composition / PDF rendering -> human review -> immutable artifact

Every rule is data in data/policies/cv_impact_ownership_v1.yaml (hash-bound to its source asset).
The composer only ever:
  * puts an action verb in front of a noun-fragment opening when the evidence is the candidate's
    own project write-up (the verb comes from the fragment's head noun, or is the fragment's own
    passive participle); everything after the noun phrase stays verbatim;
  * removes an unmeasured performance adjective from the opening (narrowing, never adding);
  * turns "Supported X by checking ..., identifying ..." into "Checked ..., identified ...,
    in support of X" (the candidate's own gerunds, the support qualifier kept);
  * appends at most one short, archetype-foregrounded value clause per project, derived from a
    mechanism stated in that same bullet's evidence.
A rewrite that loses a metric, a number, a qualifier, or introduces any overclaim, escalation verb
or grounding rejection the verbatim evidence did not already carry is refused, and the verbatim
evidence is kept. Evidence truth stays the highest authority.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import yaml

POLICY_PATH = Path("data/policies/cv_impact_ownership_v1.yaml")
_LEVEL_RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


class PolicyIntegrityError(ValueError):
    """The policy's source asset is present but does not match the recorded hash."""


@dataclass(frozen=True)
class ValueRule:
    id: str
    requires: tuple[re.Pattern, ...]
    value: str
    level: str
    tags: tuple[str, ...]
    render: str | None
    signal: re.Pattern | None


@dataclass(frozen=True)
class MetricRule:
    type: str
    pattern: re.Pattern
    scope: str
    context: str
    not_: tuple[str, ...]


@dataclass(frozen=True)
class ImpactPolicy:
    version: str
    source_asset: str
    source_hash: str
    policy_sha256: str
    raw: dict

    @classmethod
    def load(cls, path: Path | str = POLICY_PATH) -> "ImpactPolicy":
        path = Path(path)
        data = path.read_bytes()
        raw = yaml.safe_load(data)
        source = Path(raw.get("source_asset", ""))
        if raw.get("source_asset") and source.is_file():
            if hashlib.sha256(source.read_bytes()).hexdigest() != raw.get("source_hash"):
                raise PolicyIntegrityError(f"{path} does not match its source asset {source}")
        return cls(version=str(raw.get("transformation_version", "1.0")), source_asset=raw.get("source_asset", ""),
                   source_hash=raw.get("source_hash", ""), policy_sha256=hashlib.sha256(data).hexdigest(), raw=raw)

    @property
    def spec(self) -> dict:
        return self.raw["CV_IMPACT_OWNERSHIP_V1"]

    @property
    def id(self) -> str:
        return f"cv_impact_ownership_v{self.version}@{self.policy_sha256[:12]}"

    @property
    def precedence(self) -> list[str]:
        return list(self.spec.get("precedence", []))

    # --- compiled views (cached on first use) ---

    def _cached(self, key, build):
        cache = self.__dict__.setdefault("_cache", {})
        if key not in cache:
            cache[key] = build()
        return cache[key]

    @property
    def ownership(self) -> dict:
        return self.spec["ownership"]

    @property
    def composition(self) -> dict:
        return self.spec["composition"]

    @property
    def verbs(self) -> dict[str, set[str]]:
        o = self.ownership
        return self._cached("verbs", lambda: {
            "action": {v.casefold() for v in o["action_verbs"]},
            "escalation": {v.casefold() for v in o["escalation_verbs"]},
            "collaborative": {v.casefold() for v in o["collaborative_verbs"]},
            "other": {v.casefold() for v in o["other_past_verbs"]} | {v.casefold() for v in o["irregular_past_verbs"]},
            "adjectival": {v.casefold() for v in o["adjectival_participles"]},
        })

    @property
    def head_nouns(self) -> dict[str, str]:
        return self._cached("heads", lambda: {
            noun.casefold(): verb for verb, nouns in self.composition["head_noun_verbs"].items() for noun in nouns})

    @property
    def value_rules(self) -> list[ValueRule]:
        return self._cached("values", lambda: [
            ValueRule(r["id"], tuple(re.compile(p, re.I) for p in r["requires"]), r["value"], r["level"],
                      tuple(r.get("tags", ())), r.get("render"),
                      re.compile(r["signal"], re.I) if r.get("signal") else None)
            for r in self.spec["value_translations"]])

    @property
    def metric_rules(self) -> list[MetricRule]:
        return self._cached("metrics", lambda: [
            MetricRule(r["type"], re.compile(r["pattern"], re.I), r["scope"], r["context"], tuple(r.get("not", ())))
            for r in self.spec["metrics"]["rules"]])

    @property
    def overclaim_terms(self) -> dict[str, re.Pattern]:
        return self._cached("overclaims", lambda: {
            name: re.compile(rf"(?<![\w-])(?:{pattern})(?![\w])", re.I)
            for name, pattern in self.spec["overclaims"]["terms"].items()})

    @property
    def measurement(self) -> re.Pattern:
        return self._cached("measure", lambda: re.compile(self.composition["measurement_pattern"], re.I))


def load_default_policy(path: Path | str = POLICY_PATH) -> ImpactPolicy | None:
    """The shipped policy, or None when it is absent (generation then stays purely extractive)."""
    return ImpactPolicy.load(path) if Path(path).is_file() else None


# --- metric classification --------------------------------------------------------------------

@dataclass(frozen=True)
class MetricClaim:
    text: str
    metric_type: str
    value: str
    denominator: str | None
    scope: str
    context: str
    must_not_become: tuple[str, ...]


def classify_metrics(text: str, policy: ImpactPolicy) -> list[MetricClaim]:
    """Every number-bearing span, classified by the first matching rule (spans never overlap)."""
    taken: list[tuple[int, int]] = []
    claims: list[tuple[int, MetricClaim]] = []
    for rule in policy.metric_rules:
        for match in rule.pattern.finditer(text):
            start, end = match.span()
            if any(start < e and s < end for s, e in taken):
                continue
            groups = match.groupdict()
            value = groups.get("value") or groups.get("value2") or match.group(0)
            if rule.type == "count" and _is_identifier_number(text, start, end):
                continue
            taken.append((start, end))
            claims.append((start, MetricClaim(match.group(0).strip(), rule.type, value, groups.get("denominator"),
                                              rule.scope, rule.context, rule.not_)))
    return [claim for _, claim in sorted(claims, key=lambda item: item[0])]


def _is_identifier_number(text: str, start: int, end: int) -> bool:
    """Digits that are part of a name (React 18, Pydantic v2, I2C, ARM Cortex-M4, Level 1)."""
    before = text[max(0, start - 8):start]
    return bool(re.search(r"(?:[A-Za-z]|\bv|\bLevel |\bReact |\bPhase |\bLayer )$", before) or
                (end < len(text) and text[end:end + 1].isalpha()))


# --- engineering value translation ------------------------------------------------------------

@dataclass(frozen=True)
class EngineeringValue:
    rule_id: str
    value: str
    level: str
    tags: tuple[str, ...]
    foregrounded: bool
    render: str | None
    signal: re.Pattern | None = field(default=None, compare=False, repr=False)


def archetype_foreground(policy: ImpactPolicy, archetype: str) -> set[str]:
    return set(policy.spec["archetypes"].get(archetype, {}).get("foreground", ()))


def impact_archetype(job_title: str, tailoring_archetype: str, policy: ImpactPolicy) -> str:
    """The value-foregrounding archetype: explicit title terms first (forward-deployed, automation),
    then the tailoring skill's archetype; backend otherwise."""
    title = (job_title or "").casefold()
    archetypes = policy.spec["archetypes"]
    for name, spec in archetypes.items():
        if any(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", title) for term in spec.get("title_terms", ())):
            return name
    for name, spec in archetypes.items():
        if tailoring_archetype in spec.get("from_tailoring", ()):
            return name
    return "backend"


def translate_value(text: str, policy: ImpactPolicy, archetype: str) -> list[EngineeringValue]:
    """Supportable engineering values for this evidence text only, strongest first: foregrounded
    for the archetype, then by ladder level (L2 before L3 before L4). Never produces L1."""
    foreground = archetype_foreground(policy, archetype)
    values = []
    for rule in policy.value_rules:
        if all(pattern.search(text) for pattern in rule.requires):
            level = rule.level
            if level == "L2" and not any(m.metric_type in {"bounded_trials", "evaluation_sample"}
                                         for m in classify_metrics(text, policy)):
                level = "L3"  # a measured outcome needs its measurement in the same evidence
            values.append(EngineeringValue(rule.id, rule.value, level, rule.tags, bool(foreground & set(rule.tags)),
                                           rule.render, rule.signal))
    return sorted(values, key=lambda v: (not v.foregrounded, _LEVEL_RANK[v.level]))


# --- overclaim review -------------------------------------------------------------------------

def review_overclaims(text: str, evidence_text: str, policy: ImpactPolicy) -> list[str]:
    """Absolute / performance / scope / ownership claims in `text` not supported by `evidence_text`.
    Measurement-class terms (high-throughput, low-latency, ...) need a measurement in the evidence too."""
    needs_measurement = set(policy.spec["overclaims"]["measurement_required"])
    measured = bool(policy.measurement.search(evidence_text or ""))
    flags = []
    for name, pattern in policy.overclaim_terms.items():
        if not pattern.search(text or ""):
            continue
        supported = bool(pattern.search(evidence_text or "")) and (name not in needs_measurement or measured)
        if not supported:
            flags.append(f"overclaim:{name}")
    return flags


# --- opening classification -------------------------------------------------------------------

def opening_kind(text: str, policy: ImpactPolicy) -> str:
    """action | collaborative | inventory | metric | noun_fragment."""
    stripped = (text or "").strip()
    lowered = stripped.casefold()
    if any(lowered.startswith(opener) for opener in policy.ownership["inventory_openers"]):
        return "inventory"
    if re.match(r"^\d", stripped):
        return "metric"
    first = _WORD.match(stripped)
    word = first.group(0).casefold() if first else ""
    verbs = policy.verbs
    if word in verbs["collaborative"]:
        return "collaborative"
    if word in verbs["action"] or word in verbs["escalation"] or word in verbs["other"]:
        return "action"
    if word.endswith("ed") and word not in verbs["adjectival"]:
        return "action"  # an unlisted "-ed" opener reads as a verb: left as written, never rewritten
    if re.match(r"^[^.:]{1,40}:\s", stripped) and not _head_noun_phrase(stripped, policy):
        return "inventory"
    return "noun_fragment"


# --- composition ------------------------------------------------------------------------------

@dataclass
class BulletRewrite:
    source_project_id: str
    evidence_ids: list[str]
    original_text: str
    rewritten_text: str
    action_verb: str | None = None
    owned_artifact: str | None = None
    technical_mechanism: str | None = None
    outcome_type: str | None = None
    value_level: str | None = None
    engineering_value: str | None = None
    metric_scope: list[dict] = field(default_factory=list)
    jd_relevance: list[str] = field(default_factory=list)
    recruiter_clarity: str = ""
    ownership_truthfulness: str = ""
    overclaim_flags: list[str] = field(default_factory=list)
    rewrite_rationale: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    review_required: bool = False
    value_rendered: bool = False
    archetype: str = ""

    @property
    def changed(self) -> bool:
        return self.rewritten_text != self.original_text

    def to_dict(self) -> dict:
        data = asdict(self)
        data["changed"] = self.changed
        return data


GroundingCheck = Callable[[str], Sequence[str]]


def compose_bullet(
    text: str,
    *,
    project_id: str,
    evidence_ids: Iterable[str],
    ownership_basis: str,
    archetype: str,
    policy: ImpactPolicy,
    grounding_check: GroundingCheck | None = None,
    evidence_partial: bool = False,
) -> BulletRewrite:
    """One bullet through composition -> value translation -> overclaim / grounding review.

    `ownership_basis`: "personal_project" (the candidate's own project write-up: execution verbs
    allowed), "experience_record" (internship statement: only the support-preserving conversion),
    anything else: wording kept, only unmeasured opening adjectives removed."""
    original = (text or "").strip()
    rewrite = BulletRewrite(project_id, sorted(evidence_ids), original, original, archetype=archetype)
    candidate, notes = _strip_unmeasured_opening(original, policy)
    rewrite.rewrite_rationale += notes
    kind = opening_kind(candidate, policy)
    if kind == "noun_fragment" and ownership_basis == "personal_project":
        converted, verb, artifact, note = _action_first(candidate, policy)
        if converted:
            candidate, rewrite.action_verb, rewrite.owned_artifact = converted, verb, artifact
            rewrite.rewrite_rationale.append(note)
        else:
            rewrite.refused.append(note)
    elif kind == "noun_fragment":
        rewrite.refused.append(f"not_strengthened:ownership_basis_{ownership_basis or 'unknown'}_does_not_prove_personal_execution")
    elif kind == "collaborative":
        converted, note = _support_by_conversion(candidate, policy)
        if converted:
            candidate = converted
            rewrite.action_verb = _WORD.match(converted).group(0)
            rewrite.rewrite_rationale.append(note)
        else:
            rewrite.refused.append(note)
    elif kind in {"metric", "inventory"}:
        rewrite.refused.append(f"kept_verbatim:{kind}_opening_has_no_safe_action_rewrite")

    problems = rewrite_problems(original, candidate, policy, grounding_check)
    if problems:
        rewrite.refused += [f"rewrite_rejected:{p}" for p in problems]
        candidate = original
        rewrite.action_verb = rewrite.owned_artifact = None
    rewrite.rewritten_text = candidate

    values = translate_value(original, policy, archetype)
    if values:
        best = min(values, key=lambda v: _LEVEL_RANK[v.level])  # strongest supportable value
        rewrite.engineering_value, rewrite.value_level = best.value, best.level
        rewrite.outcome_type = policy.spec["value_ladder"][best.level]
        rewrite.technical_mechanism = best.rule_id
    foreground = archetype_foreground(policy, archetype)
    rewrite.jd_relevance = sorted({tag for value in values for tag in value.tags} & foreground)
    rewrite.metric_scope = [{"text": m.text, "type": m.metric_type, "scope": m.scope, "context": m.context,
                             "denominator": m.denominator, "must_not_become": list(m.must_not_become)}
                            for m in classify_metrics(original, policy)]
    rewrite.overclaim_flags = review_overclaims(rewrite.rewritten_text, original_without_unmeasured(original, policy), policy)
    rewrite.recruiter_clarity = opening_kind(rewrite.rewritten_text, policy)
    rewrite.ownership_truthfulness = (
        "collaborative_preserved" if kind == "collaborative" else
        "personal_project_execution" if rewrite.action_verb and ownership_basis == "personal_project" else
        "as_evidenced")
    ownership_reworded = kind == "collaborative" and rewrite.changed
    rewrite.review_required = bool(rewrite.overclaim_flags) or ownership_reworded or (evidence_partial and rewrite.changed)
    if ownership_reworded:
        rewrite.rewrite_rationale.append("review:ownership_wording_changed_from_support_opener")
    return rewrite


def original_without_unmeasured(text: str, policy: ImpactPolicy) -> str:
    """The evidence as support for an overclaim: an unmeasured performance adjective in the
    evidence never supports itself."""
    return text if policy.measurement.search(text or "") else _remove_measurement_terms(text, policy)


def _remove_measurement_terms(text: str, policy: ImpactPolicy) -> str:
    needs = set(policy.spec["overclaims"]["measurement_required"])
    for name, pattern in policy.overclaim_terms.items():
        if name in needs:
            text = pattern.sub(" ", text or "")
    return text


def render_value_clause(rewrite: BulletRewrite, policy: ImpactPolicy, grounding_check: GroundingCheck | None,
                        evidence_partial: bool = False) -> bool:
    """Append the bullet's foregrounded value clause when it adds framing the bullet does not already
    carry and the bullet stays within the length budget. Re-validated; always human-reviewed."""
    values = [v for v in translate_value(rewrite.original_text, policy, rewrite.archetype) if v.foregrounded and v.render]
    limit = policy.composition["max_rendered_chars"]
    for value in values:
        if value.signal is not None and value.signal.search(rewrite.rewritten_text):
            continue
        candidate = rewrite.rewritten_text.rstrip().rstrip(".") + value.render + "."
        if len(candidate) > limit:
            continue
        problems = rewrite_problems(rewrite.original_text, candidate, policy, grounding_check)
        if problems:
            rewrite.refused += [f"value_clause_rejected:{value.rule_id}:{p}" for p in problems]
            continue
        rewrite.rewritten_text = candidate
        rewrite.value_rendered = True
        rewrite.engineering_value, rewrite.value_level = value.value, value.level
        rewrite.outcome_type = policy.spec["value_ladder"][value.level]
        rewrite.technical_mechanism = value.rule_id
        rewrite.review_required = True
        rewrite.rewrite_rationale.append(f"value_clause:{value.rule_id}:foregrounded_for_{rewrite.archetype}")
        return True
    return False


def rewrite_problems(original: str, rewritten: str, policy: ImpactPolicy,
                     grounding_check: GroundingCheck | None = None) -> list[str]:
    """Why `rewritten` may not replace the verbatim evidence `original` (empty: it may)."""
    if rewritten == original:
        return []
    problems = []
    low_orig, low_new = original.casefold(), rewritten.casefold()
    for metric in classify_metrics(original, policy):
        if metric.text.casefold() not in low_new:
            problems.append(f"metric_lost:{metric.text}")
    numbers = lambda s: set(re.findall(r"\d+(?:[.,/]\d+)*", s))
    if numbers(rewritten) - numbers(original):
        problems.append("number_introduced")
    if rewritten.count("%") > original.count("%"):
        problems.append("percentage_introduced")
    for term in policy.spec["metrics"]["qualifier_terms"]:
        pattern = rf"(?<![a-z]){re.escape(term)}(?![a-z])"
        if re.search(pattern, low_orig) and not re.search(pattern, low_new):
            if term == "supported" and "in support of" in low_new:
                continue
            problems.append(f"qualifier_lost:{term}")
    new_words = {w.casefold() for w in _WORD.findall(rewritten)} - {w.casefold() for w in _WORD.findall(original)}
    escalations = new_words & policy.verbs["escalation"]
    if escalations:
        problems.append("ownership_escalation:" + ",".join(sorted(escalations)))
    support = original_without_unmeasured(original, policy)
    introduced = set(review_overclaims(rewritten, support, policy)) - set(review_overclaims(original, support, policy))
    problems += sorted(introduced)
    if grounding_check is not None:
        added = set(grounding_check(rewritten)) - set(grounding_check(original))
        problems += sorted(f"grounding:{reason}" for reason in added)
    return problems


def _strip_unmeasured_opening(text: str, policy: ImpactPolicy) -> tuple[str, list[str]]:
    """Remove an opening performance/prestige adjective the same evidence never measures."""
    if policy.measurement.search(text):
        return text, []
    adjectives = sorted(policy.composition["unmeasured_opening_adjectives"], key=len, reverse=True)
    pattern = re.compile(r"^(?P<article>(?:an?|the)\s+)?(?P<adj>" + "|".join(re.escape(a) for a in adjectives) + r")\s+", re.I)
    match = pattern.match(text)
    if not match:
        return text, []
    rest = text[match.end():]
    article = match.group("article") or ""
    if article:
        rest = (("an " if rest[:1].lower() in "aeiou" else "a ") + rest)
    rest = rest[:1].upper() + rest[1:]
    return rest, [f"narrowed:removed_unmeasured_adjective:{match.group('adj').casefold()}"]


def _known_proper_tokens() -> set[str]:
    from jobintel.analysis.role_tracks import SKILL_PATTERNS
    from jobintel.matching.claim_grounding import _KNOWN_TECH
    return {t.casefold() for t in _KNOWN_TECH} | {s.casefold() for s in SKILL_PATTERNS}


def _head_noun_phrase(text: str, policy: ImpactPolicy) -> tuple[int, int, str, str | None] | None:
    """(np_start, np_end, verb, passive_participle) for a noun-fragment opening, else None."""
    comp = policy.composition
    segment_end = len(text)
    for sep in (":", ",", ";", ". "):
        index = text.find(sep)
        if index != -1:
            segment_end = min(segment_end, index)
    tokens = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text[:segment_end])]
    if not tokens:
        return None
    start_index = 1 if tokens[0][2].casefold() in {"a", "an", "the"} else 0
    if start_index >= len(tokens) or not tokens[start_index][2][:1].isalnum():
        return None  # a list marker / symbol opening is not a noun phrase
    boundaries = {b.casefold() for b in comp["boundary_tokens"]}
    passives = {p.casefold() for p in comp["passive_participles"]}
    head = None
    passive = None
    for position, (s, e, token) in enumerate(tokens[start_index:], start=start_index):
        word = token.casefold().strip("()[]\"'")
        if position - start_index > comp["max_head_position"]:
            break
        if head is not None and word in passives:
            passive = word
            break
        if word in boundaries:
            break
        if word in policy.head_nouns:
            head = position
            continue
        if head is not None and word.endswith("ing"):
            break
    if head is None:
        return None
    np_start = tokens[start_index][0]
    np_end = tokens[head][1]
    verb = policy.head_nouns[tokens[head][2].casefold().strip("()[]\"'")]
    if passive is not None:
        verb = passive.capitalize()
    return np_start, np_end, verb, passive


def _action_first(text: str, policy: ImpactPolicy) -> tuple[str | None, str | None, str | None, str]:
    found = _head_noun_phrase(text, policy)
    if found is None:
        return None, None, None, "not_strengthened:no_recognised_artifact_head_noun"
    np_start, np_end, verb, passive = found
    if verb.casefold() not in policy.verbs["action"]:
        return None, None, None, f"not_strengthened:verb_{verb}_not_in_action_list"
    noun_phrase = text[np_start:np_end]
    rest = text[np_end:]
    if passive is not None:
        rest = re.sub(rf"^\s*{re.escape(passive)}\b", "", rest, flags=re.I)
    words = noun_phrase.split()
    head_word = words[-1].casefold().strip("()[]\"'")
    if verb == "Designed" and head_word in {"design", "architecture"} and len(words) > 1:
        words = words[:-1]  # "FastAPI service design" -> "Designed a FastAPI service"
        head_word = words[-1].casefold()
    generic = {term.casefold() for term in policy.composition["generic_lowercase_terms"]}
    proper = _known_proper_tokens() - generic
    first = words[0]
    if first[:1].isupper() and first[1:].islower() and first.casefold() not in proper and not any(c.isdigit() for c in first):
        words[0] = first.lower()
    noun_phrase = " ".join(words)
    comp = policy.composition
    if head_word in {n.casefold() for n in comp["mass_nouns"]} or head_word in {n.casefold() for n in comp["plural_nouns"]}:
        article = ""
    else:
        article = _article(words[0]) + " "
    converted = f"{verb} {article}{noun_phrase}{rest}"
    return converted, verb, noun_phrase, f"action_first:{verb.casefold()}_from_head_noun:{head_word}" + (
        f":passive_{passive}" if passive else "")


def _article(word: str) -> str:
    bare = word.strip("()[]\"'")
    if len(bare) >= 2 and bare.isupper() or re.match(r"^[A-Z][0-9]", bare):
        return "an" if bare[0] in "AEFHILMNORSX" else "a"
    return "an" if bare[:1].lower() in "aeiou" else "a"


def _support_by_conversion(text: str, policy: ImpactPolicy) -> tuple[str | None, str]:
    """"Supported X by checking A, identifying B" -> "Checked A, identified B, in support of X"."""
    match = re.match(policy.composition["support_by_pattern"], text)
    if not match or match.group("opener") != "Supported":
        return None, "not_strengthened:collaborative_ownership_preserved"
    gerunds = policy.composition["gerund_past"]
    acts = match.group("acts")
    unknown = []

    def convert(m: re.Match) -> str:
        word = m.group(2)
        if word not in gerunds:
            unknown.append(word)
            return m.group(0)
        return m.group(1) + gerunds[word]

    converted = re.sub(r"(^|, |\band )([a-z]+ing)\b", convert, acts)
    if unknown:
        return None, "not_strengthened:unknown_activity_" + ",".join(unknown)
    converted = converted[:1].upper() + converted[1:]
    return f"{converted}, in support of {match.group('object')}.", "action_first:support_by_activities_kept_support_qualifier"


# --- project-level composition ----------------------------------------------------------------

@dataclass(frozen=True)
class ProjectItem:
    """One candidate bullet of a project: verbatim evidence text, its evidence ids, its selection
    weight, whether that evidence is partial, and the claim-grounding check for ITS cited evidence."""
    text: str
    evidence_ids: Sequence[str]
    weight: float
    partial: bool = False
    grounding_check: GroundingCheck | None = None


@dataclass
class ComposedItem:
    index: int  # position of the source ProjectItem
    rewrite: BulletRewrite
    weight: float
    grounding_check: GroundingCheck | None = None


def compose_project(
    items: Sequence[ProjectItem],
    *,
    project_id: str,
    ownership_basis: str,
    archetype: str,
    policy: ImpactPolicy,
) -> list[ComposedItem]:
    """Compose every candidate bullet of one project, re-weight by archetype-foregrounded value,
    put an action-first delivery bullet first when one is nearly as strong, and render at most the
    policy's number of value clauses. Returned strongest first."""
    comp = policy.composition
    foreground = archetype_foreground(policy, archetype)
    composed = []
    for index, item in enumerate(items):
        rewrite = compose_bullet(item.text, project_id=project_id, evidence_ids=item.evidence_ids, ownership_basis=ownership_basis,
                                 archetype=archetype, policy=policy, grounding_check=item.grounding_check,
                                 evidence_partial=item.partial)
        tags = {tag for value in translate_value(item.text, policy, archetype) for tag in value.tags} & foreground
        factor = 1.0 + min(comp["foreground_boost_cap"], comp["foreground_boost_per_tag"] * len(tags))
        if rewrite.recruiter_clarity in {"noun_fragment", "inventory"}:
            factor *= comp["unconverted_fragment_factor"]
        composed.append(ComposedItem(index, rewrite, item.weight * factor, item.grounding_check))
    if not composed:
        return composed
    composed.sort(key=lambda entry: -entry.weight)
    top = composed[0]
    delivery = delivery_verbs(policy)
    if not _is_delivery(top.rewrite, delivery):
        for entry in composed[1:]:
            if _is_delivery(entry.rewrite, delivery) and entry.weight >= comp["first_bullet_promotion_share"] * top.weight:
                entry.weight = top.weight * 1.0001
                entry.rewrite.rewrite_rationale.append("ordered_first:states_what_was_built")
                composed.sort(key=lambda other: -other.weight)
                break
    rendered = 0
    for entry in composed:
        if rendered >= comp["max_value_clauses_per_project"]:
            break
        if entry.rewrite.recruiter_clarity == "action" and render_value_clause(entry.rewrite, policy, entry.grounding_check):
            rendered += 1
    return composed


def delivery_verbs(policy: ImpactPolicy) -> set[str]:
    """Verbs that say what was built or delivered (first-bullet ownership clarity)."""
    return {verb.casefold() for verb in policy.composition["delivery_verbs"]}


def _is_delivery(rewrite: BulletRewrite, verbs: set[str]) -> bool:
    return _states_delivery(rewrite.rewritten_text, verbs)


def _states_delivery(text: str, verbs: set[str]) -> bool:
    first = _WORD.match(text or "")
    return bool(first) and first.group(0).casefold() in verbs and len(text.split()) >= 3


def vary_repeated_verbs(rewrites: Sequence[BulletRewrite], policy: ImpactPolicy) -> list[BulletRewrite]:
    """Rotate composer-inserted interchangeable verbs (Built / Developed / Implemented) so none opens
    more than quality_gate.max_verb_repeats lines. Only verbs the composer itself inserted change;
    `rewrites` are in page-priority order. Returns the rewrites whose text changed."""
    interchangeable = list(policy.composition["interchangeable_verbs"])
    limit = policy.spec["quality_gate"]["max_verb_repeats"]
    counts: dict[str, int] = {}
    for rewrite in rewrites:
        first = _WORD.match(rewrite.rewritten_text or "")
        if first:
            counts[first.group(0)] = counts.get(first.group(0), 0) + 1
    changed = []
    seen: dict[str, int] = {}
    for rewrite in rewrites:
        verb = rewrite.action_verb
        if verb not in interchangeable or not rewrite.rewritten_text.startswith(verb + " "):
            continue
        seen[verb] = seen.get(verb, 0) + 1
        if seen[verb] <= limit or counts[verb] <= limit:
            continue
        alternative = min((v for v in interchangeable if v != verb), key=lambda v: counts.get(v, 0))
        if counts.get(alternative, 0) >= limit:
            continue
        rewrite.rewritten_text = alternative + rewrite.rewritten_text[len(verb):]
        counts[verb] -= 1
        counts[alternative] = counts.get(alternative, 0) + 1
        rewrite.rewrite_rationale.append(f"verb_variety:{verb.casefold()}->{alternative.casefold()}")
        rewrite.action_verb = alternative
        changed.append(rewrite)
    return changed


# --- summary narrowing ------------------------------------------------------------------------

def narrow_summary(summary: str | None, grounded_corpus: str, policy: ImpactPolicy) -> tuple[str | None, list[str], list[str]]:
    """Drop enumeration items of the authored summary that claim an unsupported scope/performance
    (narrowing only); report generic phrasing. Returns (summary, removed_items, generic_flags)."""
    if not summary:
        return summary, [], []
    spec = policy.spec["summary"]
    corpus = (grounded_corpus or "").casefold()
    text, removed = summary, []
    for item in spec["unsupported_scope_items"]:
        item_re = re.compile(rf"(?<![\w-]){item}(?![\w])", re.I)
        found = item_re.search(text)
        if not found or item_re.search(corpus):
            continue
        phrase = found.group(0)
        for pattern, replacement in ((rf",\s*{re.escape(phrase)}(?=[,.;])", ""), (rf"{re.escape(phrase)},\s*", ""),
                                     (rf",?\s+and\s+{re.escape(phrase)}(?=[.;])", "")):
            narrowed, count = re.subn(pattern, replacement, text, count=1)
            if count:
                text = narrowed
                removed.append(phrase)
                break
    generic = [m.group(0) for p in spec["generic_phrases"] for m in [re.search(rf"\b{p}\b", text, re.I)] if m]
    return text, removed, generic


# --- writing quality gate ---------------------------------------------------------------------

def writing_quality_report(
    projects: Sequence[tuple[str, Sequence[tuple[str, Sequence[str]]]]],
    experience: Sequence[str],
    summary: str | None,
    policy: ImpactPolicy,
    *,
    project_evidence_text: dict[str, str] | None = None,
    project_allowed_tech: dict[str, set[str]] | None = None,
    repo_text: dict[str, str | None] | None = None,
    rewrites: Sequence[BulletRewrite] = (),
) -> dict[str, Any]:
    """Diagnostics over the final document. Never a licence to rewrite truth."""
    from jobintel.matching.claim_grounding import extract_tech_entities
    gate = policy.spec["quality_gate"]
    project_evidence_text = project_evidence_text or {}
    bullets = [(pid, text, list(ids)) for pid, items in projects for text, ids in items]
    all_lines = [text for _, text, _ in bullets] + list(experience)
    kinds = [opening_kind(text, policy) for text in all_lines]
    total = len(all_lines) or 1
    count = lambda k: sum(1 for kind in kinds if kind == k)
    first_ownership = {}
    for pid, items in projects:
        if items:
            first = items[0][0]
            first_ownership[pid] = _states_delivery(first, delivery_verbs(policy))
    metrics = [{"project_id": pid, "evidence_ids": ids, "text": m.text, "type": m.metric_type, "scope": m.scope,
                "context": m.context, "denominator": m.denominator}
               for pid, text, ids in bullets for m in classify_metrics(text, policy) if m.metric_type != "count"]
    delivery = sum(1 for pid, text, _ in bullets
                   if any(_LEVEL_RANK[v.level] <= 3 for v in translate_value(text, policy, "backend"))
                   or any(m.metric_type in {"bounded_trials", "test_count", "evaluation_sample"} for m in classify_metrics(text, policy)))
    verbs: dict[str, int] = {}
    for text, kind in zip(all_lines, kinds):
        if kind in {"action", "collaborative"}:
            verb = _WORD.match(text).group(0)
            verbs[verb] = verbs.get(verb, 0) + 1
    repeated_verbs = {verb: n for verb, n in verbs.items() if n > gate["max_verb_repeats"]}
    n = gate["repeated_ngram"]
    seen: dict[tuple, int] = {}
    repeated = set()
    for index, text in enumerate(all_lines):
        words = [w.casefold() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+#./-]*", text) if len(w) > 2]
        for gram in {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}:
            if gram in seen and seen[gram] != index:
                repeated.add(" ".join(gram))
            seen.setdefault(gram, index)
    mechanism = re.compile(r"\b(?:" + "|".join(gate["mechanism_terms"]) + r")", re.I)
    generic = [{"text": text, "phrase": m.group(0)} for text in all_lines + ([summary] if summary else [])
               for p in gate["generic_value_language"] for m in [re.search(rf"\b{p}\b", text, re.I)]
               if m and not mechanism.search(text)]
    absolute = []
    for pid, text, _ in bullets:
        support = original_without_unmeasured(project_evidence_text.get(pid, ""), policy)
        absolute += [{"project_id": pid, "text": text, "flag": f} for f in review_overclaims(text, support, policy)]
    absolute += [{"project_id": None, "text": line, "flag": f} for line in list(experience) + ([summary] if summary else [])
                 for f in review_overclaims(line, "", policy)]
    escalation = [{"text": text, "verb": _WORD.match(text).group(0)} for text in all_lines
                  if _WORD.match(text or "") and _WORD.match(text).group(0).casefold() in policy.verbs["escalation"]]
    leakage = []
    concepts = {term.casefold() for term in policy.composition["generic_lowercase_terms"]}  # concepts, not technologies
    for pid, text, _ in bullets:
        allowed = (project_allowed_tech or {}).get(pid)
        if allowed is None:
            continue
        leakage += [{"project_id": pid, "technology": tech, "text": text}
                    for tech in sorted(extract_tech_entities(text) - concepts) if tech not in allowed]
    corroboration = []
    if repo_text is not None:
        for pid, text, _ in bullets:
            for term in gate["corroboration_terms"]:
                term_re = re.compile(rf"(?<![\w-]){term}(?![\w])" if not term.startswith("\\b") else term, re.I)
                found = term_re.search(text)
                if not found:
                    continue
                readme = repo_text.get(pid)
                status = ("cache_unavailable" if readme is None else
                          "corroborated" if term_re.search(readme) else "not_found_in_cached_readme")
                corroboration.append({"project_id": pid, "claim": found.group(0), "status": status})
    lost = [{"project_id": r.source_project_id, "reason": reason} for r in rewrites for reason in r.refused
            if "qualifier_lost" in reason]
    return {
        "policy": policy.id,
        "lines_total": len(all_lines),
        "project_bullets": len(bullets),
        "action_openings": count("action"),
        "collaborative_openings": count("collaborative"),
        "noun_fragment_openings": count("noun_fragment"),
        "inventory_openings": count("inventory"),
        "metric_first_openings": count("metric"),
        "action_opening_ratio": round((count("action") + count("collaborative")) / total, 3),
        "noun_fragment_ratio": round((count("noun_fragment") + count("inventory")) / total, 3),
        "first_bullet_ownership": first_ownership,
        "delivery_or_outcome_bullets": delivery,
        "metrics": metrics,
        "repeated_verbs": repeated_verbs,
        "repeated_phrases": sorted(repeated),
        "generic_value_language": generic,
        "unsupported_absolute_claims": absolute,
        "ownership_escalation_risk": escalation,
        "cross_project_leakage": leakage,
        "lost_qualifiers": lost,
        "corroboration": corroboration,
        "rewrites": {"applied": sum(1 for r in rewrites if r.changed),
                     "refused_or_kept": sum(1 for r in rewrites if not r.changed),
                     "value_clauses": sum(1 for r in rewrites if r.value_rendered),
                     "review_required": sum(1 for r in rewrites if r.review_required)},
    }


def writing_review_reasons(report: dict, rewrites: Sequence[BulletRewrite]) -> list[str]:
    """Review-gate reasons: rewritten claims that need a human, unsupported claims, uncorroborated
    exact-repo claims, cross-project leakage and ownership-escalation wording."""
    reasons = []
    flagged = [r for r in rewrites if r.review_required and r.changed]
    if flagged:
        kinds = sorted({"value framing" if r.value_rendered else "ownership wording" if r.ownership_truthfulness ==
                        "collaborative_preserved" else "rewrite on partial evidence / overclaim" for r in flagged})
        reasons.append(f"Impact/ownership rewrite(s) need human review: {len(flagged)} line(s) ({', '.join(kinds)})")
    if report.get("unsupported_absolute_claims"):
        flags = sorted({item["flag"].split(":", 1)[1] for item in report["unsupported_absolute_claims"]})
        reasons.append(f"Unsupported absolute/performance wording present: {', '.join(flags)}")
    missing = sorted({f"{c['project_id']}:{c['claim']}" for c in report.get("corroboration", [])
                      if c["status"] == "not_found_in_cached_readme"})
    if missing:
        reasons.append("Claim(s) not found in the project's own cached repository README -- verify before use: "
                       + ", ".join(missing))
    if report.get("cross_project_leakage"):
        leaks = sorted({f"{x['project_id']}:{x['technology']}" for x in report["cross_project_leakage"]})
        reasons.append("Technology not evidenced by the bullet's own project: " + ", ".join(leaks))
    if report.get("ownership_escalation_risk"):
        reasons.append("Ownership-escalation verb(s) present: " + ", ".join(
            sorted({x["verb"] for x in report["ownership_escalation_risk"]})))
    return reasons


def repo_readme_text(store_root: Path | str, repository_full_name: str | None) -> str | None:
    """The project's own cached repository README (exact repository name only; no fuzzy match)."""
    if not repository_full_name or "/" not in repository_full_name:
        return None
    owner, name = repository_full_name.split("/", 1)
    path = Path(store_root) / "profile" / "github_cache" / f"{owner}__{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
