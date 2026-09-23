"""The answer memory: what the candidate has answered, where it came from, and what it is worth.

answers.json used to be a flat {normalised question -> answer} map, which made a human's own answer, a
model's guess and a dropdown nobody ever touched indistinguishable and equally authoritative. They are
not the same thing: a value scraped off an untouched country list must never settle a question about
citizenship, and an answer the candidate typed must outrank anything the model produced. This module owns
the record, the provenance ladder, and the on-disk format.

Contract (do not change signatures without updating all callers):
    SOURCES / TRUST / CONFIDENCE              # the provenance ladder
    AnswerRecord                              # one remembered answer
    normalize_question(q) -> str              # the canonical key (re-exported by apply.resolver)
    load() -> dict[str, AnswerRecord]         # answers.json, upgrading a v1 file on the way
    save(records) -> None
    save_merged(records) -> dict[str, AnswerRecord]
    from_mapping(d, default_source) -> dict[str, AnswerRecord]
    text_view(records) -> dict[str, str]      # the flat map config.load_answers and the Settings box show
    apply_text_edit(records, edited) -> dict[str, AnswerRecord]
    classify_legacy(key, value) -> tuple[str, str]      # (confidence, note)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

VERSION = 2

# Where an answer came from, best first. A record only ever beats another record of lower trust.
#   human   the candidate answered a pause in the web UI, or edited the Settings box
#   facts   read out of facts.yaml
#   typed   the candidate typed it into the browser window by hand and jobbot harvested it
#   legacy  came out of the old flat file, so its real origin is unknown
#   rule    derived from facts.yaml by a built-in rule
#   llm     the model wrote it
#   scraped read off a control that nobody demonstrably touched
SOURCES = ("human", "facts", "typed", "legacy", "rule", "llm", "scraped")
TRUST = {"human": 5, "facts": 4, "typed": 3, "legacy": 3, "rule": 2, "llm": 1, "scraped": 0}

#   confirmed    survived onto an application that was actually submitted
#   provisional  believed, but not yet proven on a real submit
#   quarantined  not used, kept so the candidate can look at it and decide
#   rejected     the form refused it; never replay it
CONFIDENCE = ("confirmed", "provisional", "quarantined", "rejected")
USABLE = ("confirmed", "provisional")

_TRAILERS = re.compile(r"(\s*\*+\s*|\s*\(\s*required\s*\)\s*|\s*\(\s*optional\s*\)\s*|\s*required\s*$|\s*optional\s*$)+$", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_question(q: str) -> str:
    """lowercase, strip trailing '*' / '(required)' / '(optional)', strip punctuation, collapse whitespace."""
    s = (q or "").strip()
    s = _TRAILERS.sub("", s)
    s = s.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


@lru_cache(maxsize=16)
def kw_regex(words: frozenset[str] | tuple[str, ...]) -> re.Pattern:
    """Keyword matcher anchored on word starts.

    Plain substring matching mis-fires: 'age ' matched inside 'stor*age* ' and routed a question about data
    architectures to the protected-question path. Keywords are prefixes by design ('authoriz' must catch
    'authorized'/'authorization'), so anchor the start only — except entries written with a trailing space,
    which mean whole-word ('age', not 'agenda').
    """
    parts = []
    for w in sorted(words):
        stripped = w.strip()
        if not stripped:
            continue
        tail = r"\b" if w != stripped else ""
        parts.append(r"\b" + re.escape(stripped) + tail)
    return re.compile("|".join(parts), re.I)


# ---------- what a question is allowed to be answered from ----------
# Legal self-identifications. facts.yaml said it or the candidate says it: never the cache, never the model.
EEO_KEYWORDS: tuple[str, ...] = (
    "gender", "pronoun ", "pronouns ", "race", "ethnic", "veteran", "disabilit", "sexual orientation",
    "lgbt", "hispanic", "latino", "religion", "transgender", "military status", "date of birth",
)

# Personal details that must come from facts or the candidate — a model guessing a phone number or an
# address puts a fabricated contact detail on a real application.
NEVER_GUESS: tuple[str, ...] = ("phone", "mobile", "telephone", "contact number", "email", "e mail",
                                "date of birth", "postal", "postcode", "zip code", "street address",
                                "national id", "passport", "social security", "ssn")

# What was studied, and where. One candidate has one answer to these and it never changes, so the danger
# is not a stale answer — it is a confidently wrong one that is then never questioned again.
#
# Both ways of getting one wrong have already happened. The cache learned "Accounting" as the field of
# study from a Southern Cross form on 2026-09-20 — a discipline list resting on its first alphabetical
# option, the same shape of mistake as the country list that taught it "Afghanistan" — and tagged it as
# the candidate's own answer, so every application afterwards offered Accounting to employers reading a
# Computer Science CV. The model, asked the same thing, read the CV's "B.Sc. in Computer Science &
# Engineering" and put the subject into the Degree box. facts.yaml has an education block for exactly
# this; nothing else may answer these.
EDUCATION_KEYWORDS: frozenset[str] = frozenset({
    "field of study", "field of degree", "discipline", "major", "course of study", "area of study",
    "subject of study", "school", "university", "college", "institution", "alma mater",
    "degree", "qualification", "education level", "level of education", "highest education",
})

# Declarations scoped to a country or a law. The answer changes with the employer, so a remembered one is
# worse than no answer at all: "No" to the right to work in Australia is not an answer about Canada.
FACTS_ONLY_KEYWORDS: frozenset[str] = frozenset({
    "authoriz", "authoris", "visa", "sponsor", "citizen", "clearance", "work permit", "right to work",
    "legally", "immigration", "eligible to work", "working rights", "work rights",
    # "residency status" and "permanent resident" are the same declaration in other words. Deliberately not
    # "residence", which is where somebody lives: "Country/Region of residence" is an address field, and
    # answering it from the facts file is right.
    "residency", "permanent resident",
    # "age" is whole-word (the trailing space): as a prefix it also matched "stor*age*" and routed a
    # question about data architectures down the protected path. "military" is here as well as
    # "military status" in EEO_KEYWORDS, because a form may ask about either.
    "military", "age ",
    *EEO_KEYWORDS, *NEVER_GUESS, *EDUCATION_KEYWORDS,
})


# A figure or a preference only the candidate can set, but the same one at every employer. facts.yaml
# first, then what they have already said themselves. Still never the model.
REUSABLE_PROTECTED_KEYWORDS: frozenset[str] = frozenset({
    "salary", "compensation", "pay expectation", "expected pay", "rate expectation", "remuneration",
    "day rate",
})

# Kept as the union so is_protected() and every caller keep the meaning they had.
PROTECTED_KEYWORDS: frozenset[str] = FACTS_ONLY_KEYWORDS | REUSABLE_PROTECTED_KEYWORDS


# A question that mentions a contact detail without asking for one. Workday's "Phone Device Type" offers
# Landline or Mobile, "Phone Country Code" offers a dial code, an "Email Type" offers Work or Home: these
# are questions about the shape of the field, not the detail itself, and nothing about answering them can
# put a fabricated phone number or address on an application.
#
# It cost two applications. "phone" is in NEVER_GUESS so that a model can never invent a phone number, and
# it swept up "Phone Device Type" with it — a required Workday field whose answer is plainly Mobile, which
# the run then stopped on twice and asked the user for.
_ABOUT_THE_FIELD_RE = re.compile(
    r"\b(?:device\s*type|(?:phone|email|number|address|contact)\s*type"
    r"|type\s+of\s+(?:phone|number|email|address)|country\s*(?:phone\s*)?code|phone\s*country\s*code"
    r"|dial(?:l?ing)?\s*code|area\s*code|extension|ext\b|preferred\s+(?:contact|method)"
    r"|contact\s+method|primary\s+(?:phone|number|email))\b", re.I)
# The declarations that stay facts-only whatever else the question says: a visa, a citizenship, an EEO
# self-identification. Only the contact-detail half of the list can be waived by the rule above.
_FACTS_ONLY_DECLARATIONS: frozenset[str] = frozenset(FACTS_ONLY_KEYWORDS) - frozenset(NEVER_GUESS)


def is_about_the_field(question: str) -> bool:
    """True when the label asks about the shape of a contact detail rather than for the detail itself.

    "Phone Country Code", "Country dialing code", "Phone Device Type" — what sits in one of these is a
    property of the field beside it, never the candidate's phone number or address. Public because the
    learn-back path needs the same test: application 122 read "🇧🇩 +880" out of a box labelled
    "Country dialing code" and wrote it into facts.yaml as identity.location.
    """
    return bool(_ABOUT_THE_FIELD_RE.search(normalize_question(question)))


# "Degree" and "major" are education words in a box and ordinary English in a sentence. "To what degree do
# you agree", "a major incident", "which school of thought" are screening questions the model should answer
# normally; routing them down the facts-only path would stop the run to ask the candidate which university
# they went to. Education words only count when the question is a field label rather than a sentence.
_EDUCATION_AS_PROSE_RE = re.compile(
    r"\bto what degree\b|\bdegree (?:of|to which)\b|\bsome degree\b|\bmajor(?:ity| life| incident|ly)\b"
    r"|\bschool of thought\b|\bold school\b", re.I)


def is_facts_only(question: str) -> bool:
    key = normalize_question(question)
    if _ABOUT_THE_FIELD_RE.search(key) and not kw_regex(_FACTS_ONLY_DECLARATIONS).search(key):
        return False
    if (_EDUCATION_AS_PROSE_RE.search(key)
            and not kw_regex(FACTS_ONLY_KEYWORDS - EDUCATION_KEYWORDS).search(key)):
        return False
    return bool(kw_regex(FACTS_ONLY_KEYWORDS).search(key))


def is_reusable_protected(question: str) -> bool:
    return bool(kw_regex(REUSABLE_PROTECTED_KEYWORDS).search(normalize_question(question)))


# ---------- what is not an answer ----------
# A control resting on what the markup put in it. None of these is something a person chose.
PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "attach", "select", "select one", "select an option", "please select", "please choose", "choose",
    "choose one", "make a selection", "continue", "none", "nil", "n a", "na", "null", "-", "--", "---",
    "not applicable", "not specified", "none selected", "no selection", "pick one", "any",
})


def real_options(options) -> list[str]:
    """The options a person could actually mean, in the order they were offered.

    A list's own prompt ("Select One", "Please choose") is not a choice, and it must never be offered back
    to the candidate as one. This is the whole of a bug worth remembering: Workday's "Phone Device Type"
    paused for the user, the pause offered ["Select One", "Landline", "Mobile"], "Select One" was picked
    and learned as the answer — and from then on every application answered that question by selecting the
    prompt, which Workday refuses with "Select One" and will not move past. Two applications died on it.
    """
    return [o for o in (str(x) for x in (options or [])) if normalize_question(o) not in PLACEHOLDER_VALUES]


def is_placeholder(value: str) -> bool:
    """True for a value that is a list's prompt rather than an answer to anything."""
    return normalize_question(value) in PLACEHOLDER_VALUES

# Bare labels from a repeating row — an employment history entry, an education entry, a language row. The
# answer belongs to that one row, not to the candidate: replaying "Anlytic" into the next employer's
# history section fills it with the wrong job.
ROW_FIELD_LABELS: frozenset[str] = frozenset({
    # employment history
    "company", "company name", "employer", "employer name", "job title", "position", "position title",
    "role", "role description", "responsibilities", "duties", "reason for leaving", "supervisor",
    "supervisor name", "i currently work here", "i currently work in this role", "currently work here",
    "start date", "end date", "from", "to", "location",
    # education
    "degree", "degree type", "field of study", "major", "school", "school name", "university",
    "institution", "qualification", "gpa", "grade", "graduation date",
    # language rows
    "language", "i am fluent in this language", "proficiency", "language proficiency", "fluency",
    # widget artefacts that are never a screening question
    "notification", "notifications", "country region code", "country code", "area code",
})

# Format/bidi control characters. A value carrying one was rendered by a widget, never typed by a person:
# "English UK ‎(English UK)‎" is what a locale dropdown draws, not what anybody answered.
_BIDI_RE = re.compile(r"[‎‏‪-‮⁦-⁩]")

# An essay-shaped question may legitimately hold a long answer; a bare label may not.
_ESSAY_KEY_RE = re.compile(r"\bwhy\b|\bdescribe\b|\btell us\b|\bexplain\b|\bcover\s*letter\b|\bmotivation\b"
                           r"|\babout yourself\b|\bexperience with\b|\badditional information\b")
LONG_ANSWER_LIMIT = 800


def classify_legacy(key: str, value: str) -> tuple[str, str]:
    """(confidence, note) for one entry of the old flat file.

    The old file has no provenance, so every entry is judged on its shape alone. Anything that looks like a
    widget default, a repeating-row field, or a declaration that must come from facts.yaml is quarantined —
    not deleted, because only the candidate can say which of these is actually true of them.
    """
    v = (value or "").strip()
    k = normalize_question(key)
    if not v:
        return "quarantined", "empty"
    if not normalize_question(v) or normalize_question(v) in PLACEHOLDER_VALUES:
        return "quarantined", f"{v!r} is what an untouched control shows, not an answer"
    if _BIDI_RE.search(v):
        return "quarantined", "the value carries formatting characters, so a widget drew it"
    if k in ROW_FIELD_LABELS:
        return "quarantined", "a field from a repeating row, which belongs to that row and not to you"
    if is_facts_only(k):
        return "quarantined", "this must be answered from facts.yaml or by you, never from the cache"
    if len(v) > LONG_ANSWER_LIMIT and not _ESSAY_KEY_RE.search(k):
        return "quarantined", f"{len(v)} characters under a short label, so it landed in the wrong field"
    if normalize_question(v) == k:
        return "quarantined", "the value just repeats the label"
    return "provisional", ""


# ---------- the record ----------
@dataclass
class AnswerRecord:
    """One remembered answer, with where it came from and what it is worth."""
    key: str
    answer: str
    source: str = "human"
    confidence: str = "provisional"
    kind: str = ""
    options: list[str] = field(default_factory=list)
    scope: str = ""            # "" | country:<name> | company:<slug> | section:<name>   (Stage 2)
    reusable: bool = True
    job_id: str = ""
    company: str = ""
    ats: str = ""
    question: str = ""         # the raw wording, for the review table and the recall matcher
    learned_at: str = ""
    confirmed_at: str = ""
    used_count: int = 0
    derived_from: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.answer = "" if self.answer is None else str(self.answer)
        if self.source not in TRUST:
            self.source = "legacy"
        if self.confidence not in CONFIDENCE:
            self.confidence = "provisional"
        if not self.learned_at:
            self.learned_at = now()

    @property
    def trust(self) -> int:
        return TRUST.get(self.source, 0)

    @property
    def usable(self) -> bool:
        return self.confidence in USABLE

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("key", None)
        # Keep the file small and readable: only what differs from the default is written.
        for k, default in (("kind", ""), ("options", []), ("scope", ""), ("reusable", True), ("job_id", ""),
                           ("company", ""), ("ats", ""), ("question", ""), ("confirmed_at", ""),
                           ("used_count", 0), ("derived_from", ""), ("note", "")):
            if d.get(k) == default:
                d.pop(k, None)
        return d

    @classmethod
    def from_json(cls, key: str, raw: Any) -> AnswerRecord:
        if not isinstance(raw, dict):
            return cls(key=key, answer=str(raw), source="legacy")
        known = {f for f in cls.__dataclass_fields__ if f != "key"}   # noqa: SLF001 - dataclass API
        # Unknown keys are dropped rather than passed through: a hand-edited file must still load.
        return cls(key=key, **{k: v for k, v in raw.items() if k in known})


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------- the store ----------
def _path():
    from jobbot import config      # late: config imports this module's helpers in turn
    return config.ANSWERS_PATH


def from_mapping(d: dict[str, Any], default_source: str = "human") -> dict[str, AnswerRecord]:
    """Records from a flat {question: answer} map.

    `default_source` is the whole point of this function. A bare string handed to Resolver(answers=...) or
    typed into the Settings box is the candidate asserting something, so it is a human answer. The same bare
    string read out of the old file has no known origin, so it is only 'legacy'.
    """
    out: dict[str, AnswerRecord] = {}
    for q, v in (d or {}).items():
        key = normalize_question(str(q))
        if not key:
            continue
        if isinstance(v, AnswerRecord):
            out[key] = v
        else:
            out[key] = AnswerRecord(key=key, answer=str(v), source=default_source, question=str(q))
    return out


def load() -> dict[str, AnswerRecord]:
    """The store as it is on disk, upgrading and triaging a v1 flat file on the way."""
    path = _path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text() or "{}")
    except Exception as e:  # noqa: BLE001 - a corrupt file must not stop an application
        log.warning("answers.json could not be read (%s); continuing without it", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") == VERSION and isinstance(raw.get("answers"), dict):
        return {k: AnswerRecord.from_json(k, v) for k, v in raw["answers"].items()}
    return _migrate_v1(raw, path)


def _migrate_v1(flat: dict[str, Any], path) -> dict[str, AnswerRecord]:
    """Upgrade the old flat file, quarantining what cannot have been a real answer.

    Nothing is deleted. The backup is written first and only once, so the original is recoverable whatever
    the classification gets wrong.
    """
    backup = path.with_name(path.stem + ".v1.backup.json")
    if not backup.exists():
        try:
            backup.write_text(json.dumps(flat, indent=2, ensure_ascii=False, sort_keys=True))
            log.info("answers.json upgraded to v%s; the original is in %s", VERSION, backup.name)
        except Exception as e:  # noqa: BLE001
            log.warning("could not write the answers backup: %s", e)
    records: dict[str, AnswerRecord] = {}
    for q, v in flat.items():
        key = normalize_question(str(q))
        if not key:
            continue
        confidence, note = classify_legacy(key, str(v))
        records[key] = AnswerRecord(key=key, answer=str(v), source="legacy", confidence=confidence,
                                    question=str(q), note=note)
    held = sum(1 for r in records.values() if r.confidence == "quarantined")
    log.info("answers.json: %s entries upgraded, %s held for you to review", len(records), held)
    save(records)
    return records


def save(records: dict[str, AnswerRecord]) -> None:
    path = _path()
    body = {"version": VERSION,
            "answers": {k: records[k].to_json() for k in sorted(records)}}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False))


def save_merged(records: dict[str, AnswerRecord]) -> dict[str, AnswerRecord]:
    """Merge onto what is on disk and write the result back.

    Two applications run at once, each holding the store as it was when its run started. Writing that map
    whole dropped every answer the other run had learned since, so each key is merged on its own and the
    better record wins: higher trust first, then whichever was learned last.
    """
    merged = load()
    for key, rec in records.items():
        merged[key] = _better(merged.get(key), rec)
    save(merged)
    return merged


def _better(old: AnswerRecord | None, new: AnswerRecord) -> AnswerRecord:
    if old is None:
        return new
    if new.confidence == "rejected" and old.answer == new.answer:
        # A form refusing a value is the strongest thing anyone knows about it, and it has to survive the
        # merge whatever the record's provenance. Without this the branch below returned the older,
        # still-usable record and the refused answer was replayed onto the next application.
        old.confidence = "rejected"
        old.note = new.note or old.note
        return old
    if old.answer == new.answer and old.trust >= new.trust:
        # Same answer: keep the older record's provenance but carry forward anything the new one proved.
        if new.confidence == "confirmed" and old.confidence != "confirmed":
            old.confidence, old.confirmed_at = "confirmed", new.confirmed_at or now()
        old.used_count = max(old.used_count, new.used_count)
        return old
    if new.trust > old.trust:
        return new
    if new.trust < old.trust and old.usable:
        return old
    return new if new.learned_at >= old.learned_at else old


# ---------- flat views, for config and the Settings box ----------
def text_view(records: dict[str, AnswerRecord]) -> dict[str, str]:
    """The flat {question: answer} map. Only what is actually in use: a quarantined or rejected record is
    not an answer, and showing it in the box would invite it straight back onto a form."""
    return {k: r.answer for k, r in sorted(records.items()) if r.usable}


def apply_text_edit(records: dict[str, AnswerRecord], edited: dict[str, Any]) -> dict[str, AnswerRecord]:
    """The store after the candidate saved the Settings box.

    What they changed or added is theirs, so it becomes a confirmed human answer. What they left alone keeps
    the provenance it had. What they deleted goes, but a quarantined record is not in the box in the first
    place and so is never deleted by having been absent from it.
    """
    out = dict(records)
    seen: set[str] = set()
    for q, v in (edited or {}).items():
        key = normalize_question(str(q))
        if not key:
            continue
        seen.add(key)
        old = out.get(key)
        if old is not None and old.answer == str(v) and old.usable:
            continue
        out[key] = AnswerRecord(key=key, answer=str(v), source="human", confidence="confirmed",
                                question=str(q), learned_at=now(), confirmed_at=now(),
                                kind=old.kind if old else "", options=old.options if old else [],
                                scope=old.scope if old else "")
    for key, rec in list(out.items()):
        if rec.usable and key not in seen:
            del out[key]
    return out
