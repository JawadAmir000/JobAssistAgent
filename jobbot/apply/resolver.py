"""Cached-answer resolver for screening questions.

Order of resolution in Resolver.answer():
    (0) the application-source question ("how did you hear about us") -> preferences.job_source, always
    (a) exact cache hit on the normalised question
    (b) fuzzy cache hit (rapidfuzz token_set_ratio >= FUZZY_THRESHOLD)
    (c) built-in rules from facts.yaml (identity, work, authorization, preferences ...)
    (d) map rule answers onto the option list when options are given
    (e) LLM fallback (only if llm_enabled and the question is not "protected")
    otherwise -> NeedsHuman

Every successful answer is written back to answers.json via config.save_answers.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from jobbot import config
from jobbot.apply.base import NeedsHuman

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 92
FUZZY_SORT_THRESHOLD = 90

_TRAILERS = re.compile(r"(\s*\*+\s*|\s*\(\s*required\s*\)\s*|\s*\(\s*optional\s*\)\s*|\s*required\s*$|\s*optional\s*$)+$", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Decline-to-answer options, matched on shape rather than a fixed list of phrasings: the literal-substring
# version missed "I do not want to answer" — the wording of the federal disability form (CC-305) and so of
# nearly every US Greenhouse posting — because it only knew "not to answer".
_DECLINE_RE = re.compile(
    r"\bdecline\b"
    r"|\b(?:prefer|choose|wish|want|rather|would like)\s+not\b"          # "prefer not to say"
    r"|\b(?:do\s*not|don\s*'?\s*t|dont)\s+(?:wish|want|care|choose)\b"   # "I do not want to answer"
    r"|\bnot\s+to\s+(?:answer|say|disclose|self|identify|specify|state)\b",
    re.I)

_AUTH_QUESTION_RE = re.compile(r"\bsponsor|\bauthori[sz]|\bwork permit\b|\bright to work\b|\beligible to work\b|\blegally\b|\bvisa\b"
                               r"|\bwork(?:ing)?\s+rights?\b|\bimmigration\s+status\b|\bpermitted\s+to\s+work\b")
# The same subject asked as an open question ("What are your working rights in Australia?"). The answer is
# not a word but a statement of the facts, composed from facts.yaml — never from the model, which is not
# even shown the authorisation block. Yes/No-shaped questions ("Are you authorised to work in…?") stay with
# the Yes/No rules.
_AUTH_OPEN_QUESTION_RE = re.compile(
    r"\bwork(?:ing)?\s+rights?\b|\bright\s+to\s+work\b|\bwork\s+authori[sz]ation\b|\bauthori[sz]ation\s+to\s+work\b"
    r"|\bvisa\b|\bsponsor|\bwork\s+permit\b|\bimmigration\s+status\b|\beligib\w*\s+to\s+work\b")
_YES_NO_SHAPE_RE = re.compile(r"^\s*(?:do|does|did|are|is|will|would|can|could|have|has|were|should|may|must)\s+(?:you|u|i)\b")

_YES_WORDS = {"yes", "y", "true"}

# The consent gates every board puts in front of a submission. Shape-matched on the first person plus an
# agreement verb, or on the well-known notice names, so a substantive question that merely mentions
# "terms" ("Describe the terms of your notice period") is not swept up.
_CONSENT_RE = re.compile(
    r"^\s*i\s+(?:accept|agree|acknowledge|consent|confirm|certify|declare|understand|have\s+read|authori[sz]e)\b"
    r"|\b(?:accept|agree\s+to|acknowledge|consent\s+to|read\s+and\s+(?:understood|agree))\b.{0,60}"
    r"\b(?:terms|conditions|privacy|policy|notice|gdpr|data\s+(?:processing|transfer|protection|privacy)|consent)\b"
    r"|\b(?:privacy\s+(?:notice|policy|statement)|terms\s+(?:and|&)\s+conditions|point\s+of\s+data\s+transfer"
    r"|data\s+(?:processing|transfer)\s+consent|candidate\s+privacy)\b", re.I)

# The optional opt-ins that sit beside the mandatory consent: talent pools, being kept on file, recruiter
# mail. Separate from _CONSENT_RE because they are a choice rather than the price of submitting, and the
# user set that choice on 2026-09-15 — take them all, so nothing stops on a tickbox. Anything that reads as
# a statement of fact about the candidate is excluded below and still answered from facts.yaml.
_OPT_IN_RE = re.compile(
    r"\btalent\s+(?:pool|community|network|bank)\b"
    r"|\bfuture\s+(?:roles|opportunities|vacancies|positions|openings)\b"
    r"|\bkeep\s+(?:me|my\s+\w+|your\s+\w+)\b.{0,30}\bon\s+file\b|\bon\s+file\s+for\b"
    r"|\bmarketing\b|\bnewsletter\b|\bmailing\s+list\b"
    r"|\b(?:receive|send\s+me|notify\s+me|keep\s+me\s+informed)\b.{0,40}"
    r"\b(?:updates|emails|e-mails|alerts|news|opportunities|jobs)\b", re.I)

# "Tick this box if you do NOT wish to…" — the meaning inverts, so the rule must not fire. Ticking an
# opt-out is the opposite of what the setting above asks for, and there is no way to tell which from a
# keyword alone.
_OPT_OUT_RE = re.compile(r"\bdo\s+not\b|\bdon'?t\b|\bopt\b.{0,12}\bout\b|\bunsubscribe\b"
                         r"|\bwithdraw\b|\bobject\s+to\b|\bno\s+longer\b", re.I)

# Statements of fact about the candidate wear the same "I …" shape as a consent but are not one. A box
# saying "I am a veteran" or "I have the right to work here" must come from facts.yaml or the user: agreeing
# to it on their behalf puts a claim in front of an employer that may simply not be true.
_FACTUAL_CLAIM_RE = re.compile(
    r"\bsponsor(?:ship)?\b|\bvisa\b|\bwork\s+(?:authoriz|authoris|permit|right)|\bright\s+to\s+work\b"
    r"|\bcitizen|\bveteran\b|\bdisabilit|\bdisabled\b|\bgender\b|\bethnic|\brace\b"
    r"|\bcriminal\b|\bconvict|\bfelony\b|\bsecurity\s+clearance\b|\bsalary\b|\bnotice\s+period\b"
    r"|\bdate\s+of\s+birth\b|\bsexual\s+orientation\b|\bcurrently\s+employed\b", re.I)


def is_agreeable(key: str) -> bool:
    """True for a tickbox jobbot may agree to on the user's behalf without asking."""
    if not key or _OPT_OUT_RE.search(key) or _FACTUAL_CLAIM_RE.search(key):
        return False
    return bool(_CONSENT_RE.search(key) or _OPT_IN_RE.search(key))
_NO_WORDS = {"no", "n", "false"}

_LLM_TERMS = ("llm", "large language", "genai", "gen ai", "generative", "ai", "machine learning", "ml",
              "agent", "claude", "gpt", "openai", "anthropic", "prompt", "rag", "nlp", "deep learning")

# Sponsorship questions scoped to where the candidate already lives ("…to work in the country you are based").
_HOME_COUNTRY_SCOPE = re.compile(
    r"country (?:in which |where )?you (?:are |re )?(?:currently )?(?:based|located|reside|residing|live|living)"
    r"|country of residence|your current country|where you (?:currently )?(?:live|reside)")

# "How did you hear about us?" — the application-source question. Always answered from
# preferences.job_source, never asked, because the answer never varies between applications.
#
# Anchored on the question SHAPE, not on keywords: a bare "hear about" substring once answered "LinkedIn" to
# an essay prompt that said "we're keen to hear about your Kubernetes experience". The verb must be paired
# with "about/of" or with a job-shaped object, so "How did you find the interview process?" stays out.
_AUX = r"(?:did|do|does|have|had|d|ve)"
_ABOUT_VERB = (r"(?:hear|heard|learn|learned|learnt|find\s+out|found\s+out|know|knew|come\s+to\s+know"
               r"|came\s+to\s+know|get\s+to\s+know)")
_OBJ_VERB = (r"(?:find|found|see|saw|discover|discovered|come\s+across|came\s+across|locate|located"
             r"|encounter|encountered)")
_JOB_NOUN = (r"(?:job|jobs|role|position|opportunity|opening|posting|post|vacancy|listing|ad|advert"
             r"|advertisement|career|careers|company|team|organi[sz]ation|us)")
_SOURCE_QUESTION_RE = re.compile(
    # "how did you hear about us", "where did you first learn about this role", "how do you know about us"
    rf"\b(?:how|where)\s+{_AUX}\s+(?:you|u)\s+(?:first\s+|initially\s+|originally\s+)?{_ABOUT_VERB}\s+(?:about|of)\b"
    # "please tell us how you heard about this opportunity" — no auxiliary verb
    rf"|\bhow\s+(?:you|u)\s+{_ABOUT_VERB}\s+(?:about|of)\b"
    # "where did you find this job posting", "how did you come across our opening" — needs a job-ish object
    rf"|\b(?:how|where)\s+{_AUX}\s+(?:you|u)\s+(?:first\s+)?{_OBJ_VERB}\s+(?:about\s+)?"
    rf"(?:us\b|(?:this|the|our|out\s+about\s+(?:this|the|our))(?:\s+\w+){{0,2}}\s+{_JOB_NOUN}\b)"
    # "how did you become aware of this opportunity", "how were you made aware of this role"
    rf"|\bhow\s+(?:did|do|were|was|have)\s+you\s+(?:become|became|get|made)?\s*aware\s+(?:of|about)\b"
    # bare source labels used as a field name
    r"|\b(?:source\s+of\s+(?:application|applicant|referral|hire|candidate|lead|discovery)"
    r"|(?:referral|application|applicant|candidate|lead|recruit(?:ing|ment)|hire|hiring|job)\s+source"
    r"|sourcing\s+channel)\b"
    r"|\bhow\s+did\s+you\s+hear\s*$"
    r"|^(?:source|referral|heard\s+(?:about\s+us\s+)?(?:via|through|from|on)|found\s+us\s+(?:via|through|on))$")

DEFAULT_JOB_SOURCE = "LinkedIn"

# "If yes, please explain" — a follow-up that only applies when the PREVIOUS answer was the trigger.
# Scale AI's form asked this under a non-compete question answered "No", and the bot wrote a paragraph about
# visa sponsorship into it. A conditional field whose condition was not met must be left empty.
_CONDITIONAL_FOLLOWUP_RE = re.compile(
    r"^\s*if\s+(?:(?:your\s+)?answer\s+(?:is|was)\s+)?(?P<trigger>yes|no|y|n|other|so|selected|applicable|any)\b"
    r"[^a-z]*(?:please\s+)?"
    r"(?:explain|elaborate|specify|describe|provide|share|tell|give|list|detail|note|state|comment|expand)")
_AFFIRMATIVE_RE = re.compile(r"^\s*(?:yes|y|true|agree|i\s+(?:do|am|have|will))\b", re.I)
_NEGATIVE_RE = re.compile(r"^\s*(?:no|n|false|none|never|i\s+(?:do\s*n[o']?t|am\s+not|have\s+not))\b", re.I)


# One-time secrets. Cached like any other answer they would be replayed onto the next application, where a
# spent code fails the submit — and the user would have no idea why.
_ONE_TIME_RE = re.compile(
    r"\b(?:verification|security|confirmation|one\s*time|otp|access|2fa|two\s*factor)\s*(?:code|pin)\b"
    r"|\bcode\s+from\s+(?:the\s+)?(?:email|e\s*mail|sms|text|inbox)\b"
    r"|\bone\s*time\s*(?:password|passcode)\b")


def is_one_time_secret(question: str) -> bool:
    return bool(_ONE_TIME_RE.search(normalize_question(question)))


def _condition_met(trigger: str, previous: str) -> bool:
    """Does `previous` (the answer just given) satisfy this follow-up's trigger word?"""
    prev = (previous or "").strip()
    if not prev:
        return False       # nothing was answered before it; do not invent a condition
    if trigger in ("yes", "y", "so", "applicable", "any"):
        return bool(_AFFIRMATIVE_RE.match(prev))
    if trigger in ("no", "n"):
        return bool(_NEGATIVE_RE.match(prev))
    if trigger in ("other", "selected"):
        return "other" in prev.lower() or not _NEGATIVE_RE.match(prev)
    return False

# Vocabulary of a "how did you hear about us" option list. Used to recognise the question from its options
# when the label itself is uninformative ("Source", "Please select one").
_SOURCE_OPTION_RE = re.compile(
    r"linked\s*-?\s*in|indeed|glassdoor|job\s*board|jobboard|referr|recruiter|career|company\s+website"
    r"|social\s+media|twitter|facebook|instagram|google|search\s+engine|word\s+of\s+mouth|friend|colleague"
    r"|university|campus|event|conference|meetup|newsletter|blog|handshake|wellfound|angellist|hacker\s*news"
    r"|stack\s*overflow|ziprecruiter|monster|dice|builtin|otta|welcome\s+to\s+the\s+jungle|agency|headhunter"
    r"|employee|website|advert|other", re.I)
_LINKEDIN_OPTION_RE = re.compile(r"linked\s*-?\s*in", re.I)
# Fallbacks in preference order when the list has no LinkedIn entry at all. Job boards come first: these
# lists are often trees, and "Job Boards" is the branch that actually holds a LinkedIn leaf, where "Social
# Media" opens onto Facebook, Instagram, X and YouTube — none of them true.
_SOURCE_FALLBACK_RES = (
    re.compile(r"job\s*(?:board|site|search|posting|listing)|\b(?:online|internet|web)\b|search\s+engine|google", re.I),
    re.compile(r"social\s+(?:media|network)", re.I),
    re.compile(r"^\s*other\b", re.I),
)


def is_source_question(question: str) -> bool:
    """True for "how did you hear about us", by wording alone. The class method of the same name also
    weighs the options; this one is for callers that only have the label — adapters checking whether a
    control already holds an answer that policy, not the employer, is supposed to decide."""
    return bool(_SOURCE_QUESTION_RE.search(normalize_question(question)))


def _looks_like_source_options(options: list[str]) -> bool:
    """True when an option list is unmistakably a "how did you hear about us" list.

    Lets the policy fire on labels the regex cannot know about ("Source", "Please select one"), while staying
    off Yes/No, work-type and language lists: it needs a LinkedIn entry plus a majority of source words.
    """
    if not options or len(options) < 3:
        return False
    if not any(_LINKEDIN_OPTION_RE.search(o) for o in options):
        return False
    hits = sum(1 for o in options if _SOURCE_OPTION_RE.search(o))
    return hits >= max(3, len(options) // 2)


def _pick_source_option(options: list[str], source: str) -> str | None:
    """The option that best represents `source`, or None when the list has nothing close.

    Plain fuzzy matching is not enough here: "LinkedIn" scores 70 against "Linked In" and nothing at all
    against a list of ["Social media", "Online job board", "Other"], where "Social media" is the honest
    answer. So try the exact word, then spacing variants, then the categories LinkedIn belongs to.
    """
    if not options:
        return None
    sl = source.strip().lower()
    for o in options:
        if o.strip().lower() == sl:
            return o
    word = re.compile(r"\s*-?\s*".join(re.escape(c) for c in re.sub(r"\s+", "", sl)), re.I) \
        if sl else None
    if word is not None:
        for o in options:
            if word.search(o):
                return o
    for pat in _SOURCE_FALLBACK_RES:
        for o in options:
            if pat.search(o):
                return o
    return None

# "years of experience do you have" is the tail of a question about total experience, not a technology.
_GENERIC_SUBJECT = re.compile(
    r"^(experience|work|working|professional|total|relevant|industry|overall|engineering|software)\b"
    r"|\bdo you have\b")


def normalize_question(q: str) -> str:
    """lowercase, strip trailing '*' / '(required)' / '(optional)', strip punctuation, collapse whitespace."""
    s = (q or "").strip()
    s = _TRAILERS.sub("", s)
    s = s.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


@lru_cache(maxsize=8)
def _kw_regex(words: frozenset[str] | tuple[str, ...]) -> re.Pattern:
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


# Replies that are really "I don't know" wearing a suit. None of these belong on an application: when the
# model produces one, ask the user instead and cache what they say.
_NON_ANSWER_RE = re.compile(
    r"^(n\s*/?\s*a|na|none|nil|null|unknown|unsure|not\s+applicable|not\s+specified|not\s+mentioned|"
    r"not\s+available|no\s+information|no\s+idea|i\s+(?:do\s*not|don'?t)\s+know|cannot\s+answer|"
    r"can'?t\s+answer|to\s+be\s+(?:discussed|confirmed|determined)|tbd|tbc|skip(?:\s+it)?|-+|\.+)"
    r"[\s.!]*$", re.I)


# A model answering the conversation instead of the form. "I'm ready to answer the application question.
# Please provide the specific question" was cached as the answer to "Additional information" and would have
# been pasted onto every later form that asked for it. None of these shapes belong on an application.
_CHATTY_RE = re.compile(
    r"^\s*(?:unknown\b|i\s+need\s+(?:you|more|the|a)\b|i'?m\s+ready\b|i\s+am\s+ready\b|i'?d\s+be\s+happy\s+to\b"
    r"|(?:could|can|would)\s+you\s+(?:please\s+)?(?:clarify|provide|specify|share|tell)\b"
    r"|please\s+(?:provide|clarify|specify|share)\b|which\s+(?:question|field|answer)\b"
    r"|(?:as\s+an?\s+ai|i\s+am\s+an?\s+ai)\b|here\s+(?:is|are)\s+(?:my|the)\s+answer)", re.I)
_ASKS_BACK_RE = re.compile(r"\?\s*$|\b(?:clarify|specify|provide)\b[^.]*\?", re.I)


def _is_non_answer(text: str) -> bool:
    t = (text or "").strip()
    if _NON_ANSWER_RE.match(t):
        return True
    if _CHATTY_RE.match(t):
        return True
    # An answer that ends by asking the reader something is a reply to us, not to the employer.
    return bool(_ASKS_BACK_RE.search(t)) and len(t) < 400


def _is_decline_option(opt: str) -> bool:
    # ATS forms use typographic apostrophes ("don’t"); fold them so one pattern covers both.
    return bool(_DECLINE_RE.search((opt or "").replace("’", "'").replace("ʼ", "'")))


def _yes_no_option(options: list[str], want_yes: bool) -> str | None:
    words = _YES_WORDS if want_yes else _NO_WORDS
    for o in options:
        if o.strip().lower() in words:
            return o
    for o in options:
        first = normalize_question(o).split(" ")[:1]
        if first and first[0] in words:
            return o
    return None


class Resolver:
    """Answers screening questions from cache, facts, and (optionally) the LLM."""

    # Questions whose answers must NEVER be guessed by the LLM. On a miss they go to NeedsHuman.
    PROTECTED_KEYWORDS: frozenset[str] = frozenset({
        "authoriz", "authoris", "visa", "sponsor", "citizen", "clearance", "salary", "compensation",
        "work permit", "right to work", "legally", "immigration", "eligible to work", "working rights",
        "work rights",
        # EEO. "pronoun" is whole-word (the trailing space): as a prefix it also matched "pronounce", so
        # "How do you pronounce your name?" was treated as a protected EEO question and stopped the run.
        "gender", "pronoun ", "pronouns ", "race", "ethnic", "veteran", "disabilit", "sexual orientation", "lgbt",
        "hispanic", "latino", "military", "religion", "date of birth", "age ",
    })

    EEO_KEYWORDS: tuple[str, ...] = (
        "gender", "pronoun ", "pronouns ", "race", "ethnic", "veteran", "disabilit", "sexual orientation",
        "lgbt", "hispanic", "latino", "religion", "transgender", "military status", "date of birth",
    )

    # Personal details that must come from facts/cache or the user — an LLM guessing a phone number or an
    # address puts a fabricated contact detail on a real application.
    NEVER_GUESS: tuple[str, ...] = ("phone", "mobile", "telephone", "contact number", "email", "e mail",
                                    "date of birth", "postal", "postcode", "zip code", "street address",
                                    "national id", "passport", "social security", "ssn")

    def __init__(self, facts: dict, answers: dict, job: dict, llm_enabled: bool = False, cv_text: str = ""):
        self.facts = facts or {}
        self.answers: dict[str, str] = dict(answers or {})
        self.job = job or {}
        self.llm_enabled = llm_enabled
        self.cv_text = cv_text or ""
        self.previous_answer = ""   # the answer just given on this form; conditional follow-ups need it

    @classmethod
    def is_never_guess(cls, question: str) -> bool:
        return bool(_kw_regex(cls.NEVER_GUESS).search(normalize_question(question)))

    # ---------- facts helpers ----------
    def fact(self, key: str, default: Any = "") -> Any:
        cur: Any = self.facts
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    def fact_str(self, key: str, default: str = "") -> str:
        v = self.fact(key, default)
        return "" if v is None else str(v).strip()

    @property
    def job_source(self) -> str:
        """Where the candidate says they found the job. One fixed answer for every application."""
        return self.fact_str("preferences.job_source") or DEFAULT_JOB_SOURCE

    def is_source_question(self, key: str, options: list[str] | None = None) -> bool:
        return bool(_SOURCE_QUESTION_RE.search(key)) or _looks_like_source_options(options or [])

    # ---------- cache ----------
    def learn(self, question: str, answer: str) -> None:
        """Store a (question -> answer) pair in the answers cache and persist it.

        A one-time code is kept for this run only: the adapter has to read it back after the pause, but it is
        spent the moment it is used, so writing it to answers.json would poison every later application.
        """
        key = normalize_question(question)
        if not key or answer is None:
            return
        self.answers[key] = str(answer)
        if is_one_time_secret(key):
            log.info("not caching a one-time code for %r", question)
            return
        try:
            config.save_answers(self.answers)
        except Exception as e:  # pragma: no cover - disk issues shouldn't break an application run
            log.warning("could not save answers cache: %s", e)

    def knows(self, question: str) -> bool:
        """True when the cache can already answer this, so a value seen on the form teaches nothing new."""
        key = normalize_question(question)
        return bool(key) and self._cache_lookup(key) is not None

    def _cache_lookup(self, key: str) -> str | None:
        if key in self.answers:
            return self.answers[key]
        best, best_score = None, 0
        for k, v in self.answers.items():
            s = fuzz.token_set_ratio(key, k)
            # token_set_ratio scores a strict subset at 100 ("years of experience" vs "years of experience with X"),
            # so also require the sorted-token similarity to be high.
            if s >= FUZZY_THRESHOLD and fuzz.token_sort_ratio(key, k) >= FUZZY_SORT_THRESHOLD and s > best_score:
                best, best_score = v, s
        return best

    # ---------- protected / EEO ----------
    @classmethod
    def is_protected(cls, question: str) -> bool:
        return bool(_kw_regex(cls.PROTECTED_KEYWORDS).search(normalize_question(question)))

    @classmethod
    def is_eeo(cls, question: str) -> bool:
        return bool(_kw_regex(cls.EEO_KEYWORDS).search(normalize_question(question)))

    # ---------- public ----------
    def answer(self, question: str, options: list[str] | None = None, kind: str = "text") -> str:
        """Answer one form question. Remembers what it answered, so an "if yes, explain" that follows can see
        whether its condition was met."""
        ans = self._answer(question, options, kind)
        self.previous_answer = ans
        return ans

    def _answer(self, question: str, options: list[str] | None = None, kind: str = "text") -> str:
        options = [str(o) for o in (options or []) if str(o).strip()]
        key = normalize_question(question)
        if not key:
            raise NeedsHuman("Empty question", question=question, options=options, kind=kind)

        # A one-time code is never in the cache, in facts, or something a model may invent: go straight to
        # the pause. (The runner seeds it into this resolver on resume, so the cache hit below is this run's.)
        if is_one_time_secret(key) and key not in self.answers:
            raise NeedsHuman(f"Needs the code: {question}", question=question, options=options, kind=kind)

        # A conditional follow-up whose condition was not met: leave it blank rather than answering a
        # question the form never actually asked. Only for free text — a conditional dropdown does not exist.
        if kind in ("text", "textarea") and not options:
            m = _CONDITIONAL_FOLLOWUP_RE.search(key)
            if m and not _condition_met(m.group("trigger"), self.previous_answer):
                log.info("leaving %r blank: its 'if %s' condition was not met (previous answer %r)",
                         question, m.group("trigger"), self.previous_answer)
                return ""

        # (0) "How did you hear about us?" — one fixed answer, never asked, never guessed by the model.
        # Ahead of the cache on purpose: a junk answer typed once to get past a pause must not become the
        # policy for every future application.
        if self.is_source_question(key, options):
            src = self.job_source
            if options:
                pick = _pick_source_option(options, src)
                if pick is not None:
                    self.learn(question, pick)
                    return pick
                # Nothing on this list is true. Ask — never let the model pick one, which is how a form
                # asking where you heard about the job came to say YouTube: the options were Facebook,
                # Instagram, X and YouTube, and a guess among them is a false statement on a real
                # application, not a style choice.
                raise NeedsHuman(f"Needs your answer: {question}", question=question, options=options,
                                 kind=kind)
            else:
                self.learn(question, src)
                return src

        # Work authorisation depends on where the job is, so an answer cached at one employer is wrong at
        # the next: "Yes, I am currently authorized to work; I will need sponsorship in future" was learned
        # on a Bangladesh-scoped form and replayed onto an Australian one. The facts file decides these.
        if _AUTH_QUESTION_RE.search(key):
            picked = self._authorization_answer(key, options)
            if picked is not None:
                return picked
            if (not options and kind in ("text", "textarea") and _AUTH_OPEN_QUESTION_RE.search(key)
                    and not _YES_NO_SHAPE_RE.match(key)):
                stated = self._authorization_statement(key)
                if stated:
                    log.info("answering %r from the authorization facts: %r", question, stated)
                    return stated

        # (a)+(b) cache. A cached placeholder is ignored rather than replayed, so a bad answer from an earlier
        # run self-heals into a real question instead of being pasted onto every future application.
        cached = self._cache_lookup(key)
        if cached is not None and _is_non_answer(cached):
            cached = None
        if cached is not None:
            mapped = self._map_to_options(cached, options)
            if mapped is not None:
                return mapped
            if not options:
                return cached

        # EEO: answered only from the eeo block in facts.yaml, which the candidate filled in themselves.
        # Never guessed by the model, and never auto-declined — picking "I prefer not to answer" would still
        # be deciding for them. A value they have not given means the run stops and asks.
        if self.is_eeo(question):
            stated = self._eeo_answer(key)
            if stated:
                mapped = self._map_eeo_to_options(key, stated, options) if options else stated
                if mapped is not None:
                    self.learn(question, mapped)
                    return mapped
                log.info("EEO answer %r does not match any option for %r; asking", stated, question)
            raise NeedsHuman(f"Your answer needed: {question}", question=question, options=options, kind=kind)

        # (c) rules
        rule = self._rule_answer(key, question)
        if rule is not None:
            # (d) map to options
            mapped = self._map_to_options(rule, options)
            if mapped is not None:
                self.learn(question, mapped)
                return mapped
            if not options:
                self.learn(question, rule)
                return rule
            # rule produced a value that isn't among the offered options
            if self.is_protected(question):
                raise NeedsHuman(f"Could not map answer {rule!r} to the options for: {question}",
                                 question=question, options=options, kind=kind)

        # (e) LLM fallback. Work authorisation and EEO stay facts-only: those answers are legal declarations
        # and a wrong one is a false statement on a real application, not a style slip.
        if self.is_protected(question) or self.is_never_guess(question):
            raise NeedsHuman(f"Needs your answer (protected question): {question}",
                             question=question, options=options, kind=kind)
        if self.llm_enabled:
            llm = self._llm_answer(question, options)
            if llm is not None:
                self.learn(question, llm)
                return llm
        raise NeedsHuman(f"Needs your answer: {question}", question=question, options=options, kind=kind)

    # ---------- work authorisation, from facts only ----------
    def _authorization_answer(self, key: str, options: list[str]) -> str | None:
        """The option (or word) that states this candidate's authorisation truthfully, or None to fall
        through to the rules and, for a protected question, the user.

        Option lists here are sentences that combine two facts — whether the candidate may work there now,
        and whether they will need sponsorship — and a Yes/No mapper that takes the first option starting
        with "Yes" picks the wrong sentence half the time. Each option is scored on both facts instead.
        """
        needs_sponsorship = self.fact("authorization.requires_sponsorship", None)
        if needs_sponsorship is None:
            return None
        needs_sponsorship = bool(needs_sponsorship)
        authorized_now = self._authorized_here(key)
        if not options:
            return None
        if len(options) <= 2 and all(normalize_question(o) in _YES_WORDS | _NO_WORDS for o in options):
            return None       # a plain Yes/No: the rules already answer it
        scored: list[tuple[int, str]] = []
        for o in options:
            low = " " + normalize_question(o) + " "
            if _is_decline_option(o) or "unknown" in low or "not sure" in low:
                continue
            score = 0
            says_sponsor = "sponsor" in low
            sponsor_negated = bool(re.search(r"\b(?:not|no|never|without)\b[^.]{0,40}sponsor|sponsor[^.]{0,30}\b(?:not|no)\b"
                                             r"|will not require|do not require|don t require|dont require|not need", low))
            if says_sponsor:
                score += 2 if (not sponsor_negated) == needs_sponsorship else -3
            # A claim about being authorised ("I am (not) currently authorized", "citizen, permanent
            # resident"), as opposed to the noun in "my work authorisation requires sponsorship".
            says_auth = bool(re.search(r"\b(?:i am|i m|am|are|currently|legally)\s+(?:not\s+)?(?:currently\s+)?(?:legally\s+)?"
                                       r"(?:authori[sz]ed|eligible|permitted|allowed)\b|\b(?:citizen|permanent resident|right to work)\b"
                                       r"|\bunauthori[sz]ed\b", low))
            auth_negated = bool(re.search(r"\b(?:not|no)\b[^.]{0,30}(?:authori[sz]ed|eligible|permitted|allowed|right to work)"
                                          r"|\bunauthori[sz]ed\b|\bno (?:work )?authori", low))
            if says_auth and authorized_now is not None:
                score += 1 if (not auth_negated) == authorized_now else -2
            scored.append((score, o))
        if not scored:
            return None
        scored.sort(key=lambda t: -t[0])
        best, best_score = scored[0][1], scored[0][0]
        if best_score <= 0 or (len(scored) > 1 and scored[1][0] == best_score):
            return None       # nothing on the list clearly states the truth: ask
        self.learn(key, best)
        return best

    def _authorization_statement(self, key: str) -> str | None:
        """The candidate's work-authorisation position for the country this question (or the job) names,
        as one or two plain first-person sentences. None when facts.yaml does not settle it.

        For an open question there is no option to pick and no word that answers it; "Yes" typed into
        "What are your working rights in Australia?" is nonsense, and a model reply is a guess about a
        legal fact. This is assembled only from the authorization block, so every clause is one the
        candidate wrote down themselves.
        """
        auth = self.fact("authorization", {}) or {}
        needs_sponsorship = auth.get("requires_sponsorship")
        citizenship = str(auth.get("citizenship") or "").strip()
        countries = [str(c).strip() for c in (auth.get("authorized_countries") or []) if str(c).strip()]
        if needs_sponsorship is None and not countries and not citizenship:
            return None
        country = self._country_in_question(key) or self._job_country()
        authorized = self._authorized_here(key)
        parts: list[str] = []
        if citizenship:
            parts.append(f"I am a citizen of {citizenship}.")
        if country and authorized:
            parts.append(f"I have the right to work in {country} and do not need visa sponsorship.")
        elif country and authorized is False:
            if needs_sponsorship:
                parts.append(f"I do not currently hold the right to work in {country} and would need visa sponsorship.")
            else:
                parts.append(f"I do not currently hold the right to work in {country}.")
        elif countries:
            where = ", ".join(countries)
            parts.append(f"I have the right to work in {where}"
                         + (" and would need visa sponsorship elsewhere." if needs_sponsorship else "."))
        elif needs_sponsorship:
            parts.append("I would need visa sponsorship.")
        return " ".join(parts) or None

    @staticmethod
    def _country_in_question(key: str) -> str:
        """The country a question names ("…working rights in Australia" -> "Australia"), '' if none."""
        try:
            from jobbot.discovery.location import canonical
        except Exception:  # noqa: BLE001
            return ""
        m = re.search(r"\b(?:in|for|within|to)\s+(?:the\s+)?([a-z][a-z ]{1,40}?)\s*(?:$|\b(?:without|on|now|currently|at|for|to|and|or|if)\b)", key)
        if not m:
            return ""
        name = canonical(m.group(1).strip())
        return name.title() if name else ""

    def _job_country(self) -> str:
        """The country the job is in, '' when it does not say (a remote posting)."""
        try:
            from jobbot.discovery.location import canonical
        except Exception:  # noqa: BLE001
            return ""
        name = canonical(str(self.job.get("location") or ""))
        return name.title() if name else ""

    def _authorized_here(self, key: str) -> bool | None:
        """Whether the candidate may already work where this question points: the country it names, else
        the job's location, else unknown."""
        auth = self.fact("authorization", {}) or {}
        countries = [str(c).lower() for c in (auth.get("authorized_countries") or [])]
        home = self.fact_str("identity.country").lower()
        if _HOME_COUNTRY_SCOPE.search(key):
            return bool(home and (home in countries or home == str(auth.get("citizenship") or "").lower()))
        text = key + " " + str(self.job.get("location") or "").lower() + " " + str(self.job.get("title") or "").lower()
        for c in countries:
            if c and re.search(rf"\b{re.escape(c)}\b", text):
                return True
        if self.job.get("location"):
            return False      # the job names a place, and it is not one on the authorised list
        return None

    # ---------- EEO, from facts only ----------
    # EEO option lists are written as full sentences ("I am not a protected veteran", "No, I don't have a
    # disability"), so a plain yes/no answer does not match any of them and the generic mapper gives up.
    # Getting this wrong is not a style slip: it puts a false legal self-identification on an application.
    _EEO_AFFIRM_RE = re.compile(r"^\s*(?:yes\b|i (?:am|identify|have)\b(?!\s*not)|one or more)", re.I)
    _EEO_NEGATE_RE = re.compile(r"^\s*(?:no\b|i (?:am|do|have)\s*n[o']?t\b|i am not\b|not a\b)", re.I)
    _GENDER_SYNONYMS = {"male": ("male", "man"), "female": ("female", "woman"),
                        "non-binary": ("non binary", "nonbinary", "non-binary")}

    def _map_eeo_to_options(self, key: str, value: str, options: list[str]) -> str | None:
        """Map a stated self-identification onto this form's wording. None when nothing matches safely."""
        if not options:
            return value or None
        generic = self._map_to_options(value, options)
        if generic is not None:
            return generic
        vl = value.strip().lower()

        # "Are you Hispanic or Latino?" is a yes/no question about one category, not a category picker.
        if "hispanic" in key or "latino" in key:
            is_hl = "hispanic" in vl or "latino" in vl
            return self._pick_eeo_polarity(options, affirmative=is_hl)

        if "gender" in key or "sex " in key:
            for canon, names in self._GENDER_SYNONYMS.items():
                if vl in names:
                    for o in options:
                        if normalize_question(o) in [normalize_question(n) for n in names]:
                            return o
            return None

        # veteran / disability: the stated value is yes or no, the options are sentences
        if vl in _YES_WORDS or vl in _NO_WORDS:
            return self._pick_eeo_polarity(options, affirmative=vl in _YES_WORDS)
        return None

    @classmethod
    def _pick_eeo_polarity(cls, options: list[str], affirmative: bool) -> str | None:
        """The option that means yes (or no), skipping the decline option, which is never chosen for them."""
        want = cls._EEO_AFFIRM_RE if affirmative else cls._EEO_NEGATE_RE
        for o in options:
            if _is_decline_option(o):
                continue
            if want.search(o.strip()):
                return o
        return None

    def _eeo_answer(self, key: str) -> str:
        """The candidate's own self-identification for this question, '' if they have not given one.

        Matched on what the question is about rather than its wording: every board phrases these differently
        ("Gender", "What is your gender identity?", "Please select your gender") and they must all resolve to
        the same stated answer, or the promise of asking once is worthless.
        """
        eeo = self.fact("eeo", {}) or {}
        k = f" {key} "
        has = lambda *terms: any(t in k for t in terms)  # noqa: E731
        if has("veteran", "military service", "armed forces", "protected veteran"):
            return str(eeo.get("veteran_status") or "").strip()
        if has("disabilit", "disabled"):
            return str(eeo.get("disability_status") or "").strip()
        if has("race", "ethnic", "hispanic", "latino"):
            return str(eeo.get("race_ethnicity") or "").strip()
        if has("gender", "sex ", "pronoun ", "pronouns "):
            return str(eeo.get("gender") or "").strip()
        return ""

    # ---------- option mapping ----------
    @staticmethod
    def _map_to_options(value: str, options: list[str]) -> str | None:
        if not options:
            return None
        v = str(value).strip()
        vl = v.lower()
        for o in options:
            if o.strip().lower() == vl:
                return o
        if vl in _YES_WORDS or vl in _NO_WORDS:
            o = _yes_no_option(options, vl in _YES_WORDS)
            if o:
                return o
        nv = normalize_question(v)
        for o in options:
            if normalize_question(o) == nv:
                return o
        # fuzzy option match (e.g. "5" vs "5+ years", "Bangladesh" vs "Bangladesh (BD)")
        best, best_score = None, 0
        for o in options:
            s = fuzz.token_set_ratio(nv, normalize_question(o))
            if s > best_score:
                best, best_score = o, s
        if best is not None and best_score >= 95:
            return best
        return None

    # ---------- rules ----------
    def _years_for(self, key: str) -> str | None:
        yrs = self.fact_str("work.years_experience")
        m = re.search(r"(?:experience|years?)\b.*\b(?:with|in|using|of|working with|building) (.+)$", key)
        if m:
            subject = m.group(1).strip()
            if _GENERIC_SUBJECT.search(subject):
                return yrs  # "how many years of experience do you have" — no particular technology asked about
            padded = f" {subject} "
            if any((f" {t} " in padded) if len(t) <= 2 else (f" {t}" in padded) for t in _LLM_TERMS):
                llm_yrs = self.fact_str("work.years_llm_experience")
                if llm_yrs:
                    return llm_yrs
            skills = [str(s).lower() for s in (self.fact("skills", []) or [])]
            if any(s in subject or subject in s for s in skills):
                return yrs
            # Unknown subject: do NOT claim the total years. Answering "6 years of Apache Spark" when the CV
            # never mentions Spark is a false claim on a real application. Fall through to the CV-grounded
            # LLM (or the user), which can say "none" honestly.
            return None
        return yrs

    def _rule_answer(self, key: str, raw: str) -> str | None:  # noqa: C901 - big but flat
        k = f" {key} "
        has = lambda *terms: any(t in k for t in terms)  # noqa: E731
        f = self.fact_str
        auth = self.fact("authorization", {}) or {}

        # --- consent boxes ("I accept", "I have read the privacy notice", "I certify the above is true") ---
        # Ticking these is the price of applying at all; there is no honest "No" that still submits. Since
        # 2026-09-15 the optional opt-ins beside them are taken too, at the user's instruction, so that
        # nothing stops on a tickbox. Ahead of every other rule, and is_agreeable keeps claims of fact out.
        if is_agreeable(key):
            return "Yes"

        # --- work authorization (facts only; NEVER guess) ---
        if has("sponsor"):
            req = auth.get("requires_sponsorship")
            if _HOME_COUNTRY_SCOPE.search(key):
                # "…sponsorship to work in the country you are based" asks about the candidate's OWN country.
                # requires_sponsorship describes moving abroad, so it must not answer this one; only an explicit
                # authorisation for the home country can. Otherwise the user is asked once and it is cached.
                home = f("identity.country").lower()
                countries = [str(x).lower() for x in (auth.get("authorized_countries") or [])]
                citizenship = str(auth.get("citizenship") or "").lower()
                if home and (home in countries or home == citizenship):
                    return "No"
                return None
            if req is None:
                return None
            return "Yes" if bool(req) else "No"
        if has("authoriz", "authoris", "legally", "right to work", "eligible to work", "work permit", "permitted to work"):
            countries = [str(c).lower() for c in (auth.get("authorized_countries") or [])]
            m = re.search(r"(?:in|for) (?:the )?([a-z][a-z ]+?)(?: without| on a| now| currently| at| on | for | to |$)", key)
            if m:
                country = m.group(1).strip()
                aliases = {"us": "united states", "usa": "united states", "u s": "united states", "uk": "united kingdom",
                           "america": "united states", "united states of america": "united states"}
                country = aliases.get(country, country)
                for c in countries:
                    c2 = aliases.get(c, c)
                    if c2 == country or c2 in country or country in c2:
                        return "Yes"
                return "No"
            # no country recognised: only answer if the list is empty (then definitely No)
            return "No" if not countries else None
        if has("citizen"):
            cit = str(auth.get("citizenship") or "").strip()
            return cit or None
        if has("clearance"):
            return None

        # --- identity ---
        if has("first name", "given name", "forename"):
            return f("identity.first_name") or None
        if has("last name", "surname", "family name"):
            return f("identity.last_name") or None
        if has("pronounce", "pronunciation", "phonetic"):
            return None   # asks how to say the name, not what it is; the model renders it phonetically
        if has("full name", "legal name", "your name", "preferred name") or key in ("name", "candidate name"):
            return f("identity.full_name") or (f("identity.first_name") + " " + f("identity.last_name")).strip() or None
        if has("email", "e mail"):
            return f("identity.email") or None
        if has("phone", "mobile", "telephone", "contact number"):
            return f("identity.phone") or None
        if has("linkedin"):
            return f("identity.linkedin") or None
        if has("github"):
            return f("identity.github") or None
        if has("portfolio", "website", "personal site", " url", "web site"):
            return f("identity.portfolio") or f("identity.github") or None

        # --- travel ---
        # Forward Deployed roles ask this on nearly every form ("trips every 6-8 weeks", "up to 25% travel",
        # "willing to travel to client sites"), and no model can know the answer, so it comes from facts.
        if has("travel", "travelling", "traveling", "business trip", "client site", "on site visit"):
            v = self.fact("preferences.willing_to_travel", None)
            if v is None:
                return None
            return "Yes" if bool(v) else "No"

        # --- relocation / remote (before location: "relocation" contains "location") ---
        if has("relocat"):
            v = self.fact("work.open_to_relocation", None)
            return None if v is None else ("Yes" if bool(v) else "No")
        if has("remote", "work from home", "wfh"):
            v = self.fact("work.open_to_remote", None)
            return None if v is None else ("Yes" if bool(v) else "No")

        # --- location ---
        if has("country"):
            return f("identity.country") or None
        if has("city", " location", "where are you based", "where do you live", "where are you located", "reside"):
            return f("identity.location") or None

        # --- work ---
        if has("current company", "current employer", "employer", "company name", "organization", "organisation", "where do you work"):
            return f("work.current_company") or None
        if has("current title", "job title", "current role", "current position", " title "):
            return f("work.current_title") or None
        if has("years") or (has("how long") and has("experience")):
            return self._years_for(key) or None
        if has("notice"):
            return f("work.notice_period") or None

        # --- preferences ---
        if has("salary", "compensation", "pay expectation", "expected pay", "rate expectation"):
            sal = f("preferences.salary_min")
            if sal:
                cur = f("preferences.salary_currency")
                return f"{sal} {cur}".strip() if cur and cur.lower() not in sal.lower() else sal
            return "Negotiable"
        if has("start date", "when can you start", "earliest start", "availability to start", "available to start", "when are you available"):
            return f("preferences.start_date") or f("work.notice_period") or None
        return None

    # ---------- LLM ----------
    PRIOR_FOR_LLM = 12

    def _relevant_prior(self, key: str) -> dict[str, str]:
        """The cached question/answer pairs most similar to `key`, best first."""
        if not self.answers:
            return {}
        scored = sorted(self.answers.items(), key=lambda kv: fuzz.token_set_ratio(key, kv[0]), reverse=True)
        return {k: v for k, v in scored[:self.PRIOR_FOR_LLM] if not is_one_time_secret(k)}

    def _llm_answer(self, question: str, options: list[str]) -> str | None:
        try:
            from jobbot.llm import complete
        except Exception as e:  # pragma: no cover
            log.warning("llm import failed: %s", e)
            return None
        compact = {
            "identity": self.fact("identity", {}),
            "work": self.fact("work", {}),
            "skills": self.fact("skills", []),
            "summary": self.fact("summary", ""),
        }
        cv = self.cv_text or ""
        # The cached answers most like this question — not an arbitrary slice of the cache.
        # answers.json is written sorted, so the old `list(...)[-12:]` handed the model whatever happened to
        # sort last. A question the user has already answered in different words ("Do you have business
        # proficiency in English and Arabic?" vs the cached "Can you work professionally in both English and
        # Arabic?") is below the fuzzy threshold, so the model is the only thing that can recognise it — and
        # it can only do that if the relevant pair is actually in front of it.
        prior = self._relevant_prior(normalize_question(question))
        prompt = (
            "You are completing a job application form on the candidate's behalf. Answer as the candidate, "
            "truthfully, using only the CV and facts below.\n\n"
            + (f"CV:\n{cv}\n\n" if cv else "")
            + f"Facts: {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}\n"
            f"Job: {self.job.get('company', '')} — {self.job.get('title', '')}\n"
            f"Previously answered by this candidate: {json.dumps(prior, ensure_ascii=False)}\n"
            f"Question: {question}\n"
            + (f"Options: {json.dumps(options, ensure_ascii=False)}\n" if options else "")
            + "Rules:\n"
              "- Reply with the answer text only, no preamble, no quotes. Never ask a question back and "
              "never explain what you would need; nobody reads this except the employer.\n"
              "- If one of the previously answered questions asks the same thing in different words, reply "
              "with that same answer. The candidate already told you; do not ask them twice.\n"
              "- When options are given, reply with exactly one option, verbatim, and always pick one: "
              "UNKNOWN is never a valid reply to a list. Choose the option that is most accurate for this "
              "candidate.\n"
              "- Yes/No questions about skills, tools or experience: answer Yes when the CV shows that "
              "experience or something closely related to it (a related tool, the same kind of system), "
              "and No when it plainly does not. Do not say UNKNOWN to these.\n"
              "- Questions about willingness (to travel, relocate, work on site, work hours, start dates, "
              "background checks): the candidate is available immediately, open to relocation, remote and "
              "regular travel, and consents to standard checks; answer accordingly.\n"
              "- Free-text answers: be concrete and specific to this candidate and job; write in first person; "
              "keep it under 120 words unless the question asks for more.\n"
              "- Never invent employers, dates, titles, degrees, certifications or numbers that are not in the "
              "CV or facts.\n"
              "- If the question asks how to pronounce the candidate's name, write a simple phonetic "
              "rendering of the name in the facts (for example 'Jane Doe' -> 'JAYN DOH'). That is derived "
              "from the name itself, not a new fact, so it is never UNKNOWN.\n"
            + f"- If the question asks how the candidate heard about or found this job or company, the "
              f"answer is {self.job_source}; with options, pick the closest one (for example 'Social media' "
              f"or 'Job board').\n"
              "- Never reply with a placeholder or an evasion: no 'N/A', 'none', 'not applicable', "
              "'I don't know', 'unsure', 'to be discussed', and never 'I prefer not to answer' or any other "
              "decline-to-answer option. A real employer reads this answer.\n"
              "- Write the way this candidate would speak: plain, direct, first person, concrete. No "
              "corporate filler, no 'I am excited to leverage my passion for', no em-dashes, no three-item "
              "lists of adjectives. Short sentences. It should read as though they typed it themselves.\n"
              "- Free-text questions with no direct answer in the CV: still answer, in the candidate's "
              "favour, from the closest thing the CV does show. Reply exactly UNKNOWN only for a personal "
              "fact the CV and facts cannot contain (an ID number, an address, a date, a salary figure, a "
              "referral name); the candidate will then be asked directly."
        )
        try:
            # Free text needs room: 120 tokens cut essay answers off mid-sentence ("…architectures that"),
            # and a truncated answer goes onto a real application. Option picks stay cheap.
            budget = 60 if options else 600
            reply = complete(prompt, purpose="answer", job_id=self.job.get("id"), max_tokens=budget)
        except RuntimeError as e:
            log.info("LLM unavailable, needs human: %s", e)
            return None
        except Exception as e:
            log.warning("LLM answer failed: %s", e)
            return None
        reply = (reply or "").strip().strip('"').strip()
        if options:
            # An option pick is one line; anything after it is the model talking to itself.
            reply = reply.splitlines()[0].strip().strip('"').strip("'").rstrip(".") if reply else ""
        if not reply or _is_non_answer(reply):
            # No usable answer: fall through to NeedsHuman so the user supplies it once and it is cached.
            # Writing "N/A" or "I don't know" onto a real application is worse than pausing.
            return None
        if options:
            return self._map_to_options(reply, options)
        return reply
