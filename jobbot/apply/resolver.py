"""Cached-answer resolver for screening questions.

Order of resolution in Resolver.answer():
    (0)   the application-source question ("how did you hear about us") -> preferences.job_source, always
    (0.5) protected questions, in _protected_answer, BEFORE the cache is consulted:
            work authorisation and EEO from facts.yaml, then the candidate's own answer to this exact
            wording; a salary or day rate the other way round. Nothing else may settle one, and there is
            no fall-through — a protected question is answered from what it is entitled to or it stops.
    (a)   exact cache hit on the normalised question
    (b)   fuzzy cache hit (rapidfuzz token_set_ratio >= FUZZY_THRESHOLD)
    (c)   built-in rules from facts.yaml (identity, work, authorization, preferences ...)
    (d)   map rule answers onto the option list when options are given
    (e)   LLM fallback (only if llm_enabled and the question is not "protected")
    otherwise -> NeedsHuman

(0.5) is above the cache on purpose. It used to sit below it, and a value scraped off a dropdown nobody
had touched therefore answered a real EEO question, while a plain Yes/No "right to work in Australia"
was served from an entry learned on another continent.

Every successful answer is written back to answers.json through jobbot.answers, with a source saying
where it came from: what may not be guessed may not be replayed either.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from jobbot import answers as store
from jobbot import config
from jobbot.answers import (TRUST, AnswerRecord, is_facts_only, is_reusable_protected, kw_regex,
                            normalize_question)
from jobbot.apply.base import NeedsHuman

# normalize_question is re-exported: it is this module's vocabulary as far as every caller is concerned,
# and jobbot.answers owns it only because the store has to key on the same thing.
__all__ = ["Resolver", "normalize_question", "is_agreeable", "is_one_time_secret", "is_source_question",
           "as_number"]

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 92
FUZZY_SORT_THRESHOLD = 90


# Decline-to-answer options, matched on shape rather than a fixed list of phrasings: the literal-substring
# version missed "I do not want to answer" — the wording of the federal disability form (CC-305) and so of
# nearly every US Greenhouse posting — because it only knew "not to answer".
_DECLINE_RE = re.compile(
    r"\bdecline\b"
    r"|\b(?:prefer|choose|wish|want|rather|would like)\s+not\b"          # "prefer not to say"
    r"|\b(?:do\s*not|don\s*'?\s*t|dont)\s+(?:wish|want|care|choose)\b"   # "I do not want to answer"
    r"|\bnot\s+to\s+(?:answer|say|disclose|self|identify|specify|state)\b",
    re.I)

_RESIDENCY_STATUS_RE = re.compile(r"\bresidenc(?:y|e)\s+status\b|\bresident\s+status\b"
                                  r"|\bstatus\s+of\s+residenc")
_JOB_COUNTRY_Q_RE = re.compile(r"country (?:where|in which) the (?:job|position|role) is (?:located|based)"
                               r"|country of the (?:job|position|role)|job location country")
_AUTH_QUESTION_RE = re.compile(r"\bsponsor|\bauthori[sz]|\bwork permit\b|\bright to work\b|\beligib\w* to work\b|\blegally\b|\bvisa\b"
                               r"|\bwork(?:ing)?\s+rights?\b|\bimmigration\s+status\b|\bpermitted\s+to\s+work\b")
# The same subject asked as an open question ("What are your working rights in Australia?"). The answer is
# not a word but a statement of the facts, composed from facts.yaml — never from the model, which is not
# even shown the authorisation block. Yes/No-shaped questions ("Are you authorised to work in…?") stay with
# the Yes/No rules.
_AUTH_OPEN_QUESTION_RE = re.compile(
    r"\bwork(?:ing)?\s+rights?\b|\bright\s+to\s+work\b|\bwork\s+authori[sz]ation\b|\bauthori[sz]ation\s+to\s+work\b"
    r"|\bvisa\b|\bsponsor|\bwork\s+permit\b|\bimmigration\s+status\b|\beligib\w*\s+to\s+work\b")
_YES_NO_SHAPE_RE = re.compile(r"^\s*(?:do|does|did|are|is|will|would|can|could|have|has|were|should|may|must)\s+(?:you|u|i)\b")
# ...unless the same box then asks for the facts behind the answer. Application 113 (Fusion5, JobAdder)
# asked "Are you a Permanent Resident or Citizen of Australia or New Zealand? If you are on a Visa please
# provide details and applicable expiry dates, or if you require sponsorship." — one text box, yes/no in
# shape only. The sponsorship rule answered the bare "Yes" it means as "yes, I need sponsorship", and on
# the page that reads as a claim to Australian or New Zealand citizenship, which is false. A question that
# asks for details is answered with the facts, not with a word.
_ASKS_FOR_DETAIL_RE = re.compile(
    r"\bplease\s+(?:provide|specify|state|explain|describe|elaborate|detail|list|give|advise|confirm|note)\b"
    r"|\b(?:provide|specify|state|explain|describe|list|give|include)\b[^.?]{0,40}"
    r"\b(?:details?|specifics?|dates?|status|type|information|reason|which|what|why)\b"
    r"|\bif\s+(?:yes|so|not|no)\b[^.?]{0,40}\b(?:please|provide|specify|explain|state|describe|which|what)\b")

# A question asking what was studied. The same list as the one the suggestion-box filler in common.py
# matches on, so a board that offers a dropdown and a board that offers a typeahead reach the same answer.
# Education lists whose own "Other" is the answer when the real one is missing (Greenhouse offers "OTHER"
# for a university it has never heard of, RUET included). Never used outside education.
_OTHER_OK_KEY_RE = re.compile(r"school|universit|college|institution|alma mater|educational establishment")
_OTHER_OPTION_RE = re.compile(r"^(?:other|others|not listed|not applicable|none of the above)(?:\s*[/(,:-].*)?$", re.I)

_DEGREE_LEVEL_KEY_RE = re.compile(r"\bdegree\b|level of education|education level|highest (?:level of )?education")
_FIELD_OF_STUDY_KEY_RE = re.compile(
    r"field of (?:study|degree)|discipline|\bmajor\b|course of study|area of study|subject of study"
    r"|specialis|specializ|concentration")

# Questions that are really a personal fact under another name. A human answer to one of these is written
# to facts.yaml under the key on the right (Resolver._remember_fact), and _rule_answer reads it back from
# there, so "CGPA", "Cumulative Grade Point Average" and "GPA (out of 4.0)" are one fact answered once —
# not three cache entries, two of them never matching (application 274, Shopee). Only facts that are the
# candidate's own and the same at every employer; nothing a law or an employer scopes (those stay in
# FACTS_ONLY_KEYWORDS and are never derived from a form).
FACT_SYNONYMS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:cumulative\s+)?(?:gpa|cgpa|grade\s+point)\b"), "education.gpa"),
    (re.compile(r"\b(?:degree|grade|honou?rs)\s+classification\b|\bclass\s+of\s+(?:degree|honou?rs)\b"),
     "education.degree_classification"),
    (re.compile(r"\bgraduation\s+year\b|\byear\s+of\s+graduation\b"), "education.end_year"),
    (re.compile(r"\bpronouns?\b"), "eeo.pronouns"),
    (re.compile(r"\b(?:post(?:al)?\s*code|postcode|zip(?:\s*code)?)\b"), "address.postcode"),
    (re.compile(r"\bstreet\s+address\b|\baddress\s+line\s*1\b|^\s*street\s*$"), "address.street"),
    (re.compile(r"^\s*(?:city|town|suburb)\s*$"), "address.city"),
    (re.compile(r"^\s*(?:state|province|region|county)\s*$"), "address.state"),
    (re.compile(r"\btime\s*zone\b"), "identity.time_zone"),
    (re.compile(r"\bpronunciation\b|\bpronounce\b|\bphonetic\b"), "identity.name_pronunciation"),
)


def fact_key_for(question: str) -> str:
    """The facts.yaml key a question is really asking for, '' when it is not a plain personal fact."""
    key = normalize_question(question)
    if not key:
        return ""
    for pattern, fact in FACT_SYNONYMS:
        if pattern.search(key):
            return fact
    return ""


_YES_WORDS = {"yes", "y", "true"}

# The consent gates every board puts in front of a submission. Shape-matched on the first person plus an
# agreement verb, or on the well-known notice names, so a substantive question that merely mentions
# "terms" ("Describe the terms of your notice period") is not swept up.
_CONSENT_RE = re.compile(
    r"^\s*i\s+(?:accept|agree|acknowledge|consent|confirm|certify|declare|understand|have\s+read|authori[sz]e)\b"
    # PDPA-style gates worded as a noun phrase: "Consent to collect, use and disclose your personal data for
    # the purpose of recruiting..." (LINE MAN Wongnai, application 277)
    r"|\bconsent\b.{0,80}\bpersonal\s+(?:data|information)\b|\bpersonal\s+(?:data|information)\b.{0,80}\bconsent\b|\bpdpa\b"
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
    r"\b(?:updates|emails|e-mails|alerts|news|opportunities|jobs|notifications?|postings?)\b"
    r"|\bhear\s+more\s+about\b", re.I)

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

# Dial codes for the countries a candidate is likely to live in, keyed by lower-case country name. Only used
# to split facts.yaml's own phone number into its national part.
_DIAL_CODES: dict[str, str] = {
    "bangladesh": "880", "india": "91", "pakistan": "92", "sri lanka": "94", "nepal": "977",
    "united states": "1", "canada": "1", "united kingdom": "44", "australia": "61", "new zealand": "64",
    "singapore": "65", "malaysia": "60", "united arab emirates": "971", "saudi arabia": "966", "qatar": "974",
    "germany": "49", "france": "33", "netherlands": "31", "ireland": "353", "spain": "34", "italy": "39",
}

# Rule answers that lists spell differently. Keyed and valued in normalize_question form.
_OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "mobile": ("cell", "cell phone", "cellular", "mobile phone", "mobile cell", "cellphone", "handy"),
    "personal": ("home", "private", "personal email"),
}

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
    r"|\bone\s*time\s*(?:password|passcode)\b"
    # A magic sign-in link is the same thing in another shape: it signs in once and then it is spent.
    r"|\b(?:sign\s*in|log\s*in|login|magic)\s*link\b")


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


# ---------- numbers ----------
# Some boxes are <input type=number>, and an answer that reads perfectly well in prose cannot go in one:
# facts.yaml gives the notice period as "None" and the salary rule gives "Negotiable". The first names a
# number (no notice is zero weeks) and is converted; the second names none and is asked for, because a
# figure the candidate never chose must not be invented onto a real application.
_NUMBER_IN_TEXT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_ZERO_WORDS_RE = re.compile(
    r"^(?:none|nil|n/?a|no|zero|nothing|no\s+notice|immediate(?:ly)?|asap|now|"
    r"available\s+(?:now|immediately)|ready\s+to\s+start)\b", re.I)
# Questions where "none" is a refusal to name a figure rather than the figure zero. Offering to work for
# nothing is not what "salary: negotiable" means.
_FIGURE_QUESTION_RE = re.compile(r"salary|compensation|\bpay\b|\brate\b|wage|remuneration|package|\bctc\b", re.I)
NUMBER_MAX_CHARS = 40   # past this it is a sentence that happens to contain a digit, not an answer


def as_number(text: str, key: str = "") -> str | None:
    """`text` as the plain number a number box will take, or None when it names no number honestly.

    "6" -> "6", "3 weeks" -> "3", "$120,000" -> "120000", "None" (notice period) -> "0",
    "Negotiable" -> None, and a paragraph that merely contains a digit -> None.
    """
    t = (text or "").strip()
    if not t or len(t) > NUMBER_MAX_CHARS:
        return None
    m = _NUMBER_IN_TEXT_RE.search(t)
    if m:
        return m.group(0).replace(",", "").lstrip("+")
    if _ZERO_WORDS_RE.match(t) and not _FIGURE_QUESTION_RE.search(key):
        return "0"
    return None


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


_NEGATED_OPTION_RE = re.compile(r"\b(?:not|no|never|decline|refuse|disagree|don'?t)\b", re.I)


# What may follow an answer inside a longer option without changing it: "(BD)", "+ years", "and engineering",
# a short code. "management" after "engineering" is a different subject, not noise.
_TRAILING_NOISE_RE = re.compile(r"^\s*(?:[(\[+&/-]|and\b|or\b|years?\b|yrs?\b|months?\b|[a-z0-9]{1,3}\s*$)")


def _extends_cleanly(value: str, option: str) -> bool:
    """True when `option` (normalized) is `value` plus trailing noise."""
    if not value or not option.startswith(value):
        return False
    rem = option[len(value):]
    return not rem.strip() or bool(_TRAILING_NOISE_RE.match(rem))


def _yes_no_option(options: list[str], want_yes: bool) -> str | None:
    words = _YES_WORDS if want_yes else _NO_WORDS
    for o in options:
        if o.strip().lower() in words:
            return o
    for o in options:
        first = normalize_question(o).split(" ")[:1]
        if first and first[0] in words:
            return o
    # A pair worded as sentences, one of them negated: "I have read the terms and hereby give my consent" /
    # "I do not give consent" (application 277). Yes is the one that is not.
    if len(options) == 2:
        negated = [bool(_NEGATED_OPTION_RE.search(o.replace("\u2019", "'"))) for o in options]
        if negated.count(True) == 1:
            return options[negated.index(not want_yes)]
    return None


class Resolver:
    """Answers screening questions from cache, facts, and (optionally) the LLM."""

    # Questions whose answers must NEVER be guessed by the LLM. On a miss they go to NeedsHuman. The sets
    # live in jobbot.answers because the store has to make the same judgement when it triages a record:
    # what may not be guessed may not be replayed out of the cache either.
    #   FACTS_ONLY        scoped to a country or a law, so a remembered answer is worse than none
    #   REUSABLE_PROTECTED  a figure only the candidate sets, but the same one at every employer
    PROTECTED_KEYWORDS: frozenset[str] = store.PROTECTED_KEYWORDS
    FACTS_ONLY_KEYWORDS: frozenset[str] = store.FACTS_ONLY_KEYWORDS
    REUSABLE_PROTECTED_KEYWORDS: frozenset[str] = store.REUSABLE_PROTECTED_KEYWORDS
    EEO_KEYWORDS: tuple[str, ...] = store.EEO_KEYWORDS
    NEVER_GUESS: tuple[str, ...] = store.NEVER_GUESS

    def __init__(self, facts: dict, answers: dict, job: dict, llm_enabled: bool = False, cv_text: str = "",
                 records: dict[str, AnswerRecord] | None = None):
        self.facts = facts or {}
        self.answers: dict[str, str] = dict(answers or {})
        # The same answers with their provenance. `answers` stays a plain {question: answer} map because
        # that is what every caller and every fuzzy scan wants; `records` is what decides whether a given
        # entry is allowed to settle a given question. A bare map with no records is the candidate
        # asserting something — a test fixture or the Settings box — so it is trusted as their own answer.
        self.records: dict[str, AnswerRecord] = dict(records or {})
        for _k, _v in self.answers.items():
            self.records.setdefault(_k, AnswerRecord(key=_k, answer=_v, source="human"))
        self.job = job or {}
        self.llm_enabled = llm_enabled
        self.cv_text = cv_text or ""
        self.previous_answer = ""   # the answer just given on this form; conditional follow-ups need it

    @classmethod
    def is_never_guess(cls, question: str) -> bool:
        return bool(kw_regex(cls.NEVER_GUESS).search(normalize_question(question)))

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
    def learn(self, question: str, answer: str, *, source: str = "human", kind: str = "",
              options: list[str] | None = None, scope: str = "", confidence: str = "provisional",
              reusable: bool = True) -> None:
        """Store a (question -> answer) pair in the answers cache and persist it.

        `source` is what makes the entry worth anything later. It defaults to "human" because the call that
        matters most — the candidate answering a pause in the web UI — comes in through runner with no
        keyword at all. Everything jobbot worked out for itself says so: "rule" from facts.yaml, "llm" from
        the model, "typed" for a value harvested off the form.

        A one-time code is kept for this run only: the adapter has to read it back after the pause, but it is
        spent the moment it is used, so writing it to answers.json would poison every later application.
        """
        key = normalize_question(question)
        if not key or answer is None:
            return
        if store.is_placeholder(str(answer)):
            # Not an answer, whoever supplied it. Kept out of memory as well as off the disk: a run that
            # accepted one would select the list's own prompt on the form, and the board refuses that.
            log.info("not learning %r for %r: that is the list's prompt, not an answer", str(answer), question)
            return
        rec = AnswerRecord(key=key, answer=str(answer), source=source, confidence=confidence, kind=kind,
                           options=[str(o) for o in (options or [])], scope=scope, reusable=reusable,
                           question=str(question), job_id=str(self.job.get("id", "") or ""),
                           company=str(self.job.get("company", "") or ""),
                           ats=str(self.job.get("ats", "") or ""))
        self.answers[key] = rec.answer
        self.records[key] = rec
        if is_one_time_secret(key):
            log.info("not caching a one-time code for %r", question)
            return
        if source == "human":
            # A personal fact the candidate has just stated (a CGPA, a postcode) is worth more than a
            # remembered wording: written to facts.yaml under the key the rules read, it answers every
            # later form however that form words the question. See FACT_SYNONYMS.
            self._remember_fact(question, rec.answer)
        try:
            # Merged onto what is on disk, never written over it. This resolver holds the cache as it was
            # when its run started, and two applications run at once: saving its own map whole dropped
            # every answer the other run had learned since.
            config.save_answer_records({key: rec})
        except Exception as e:  # pragma: no cover - disk issues shouldn't break an application run
            log.warning("could not save answers cache: %s", e)

    def _remember_fact(self, question: str, answer: str) -> None:
        key = fact_key_for(question)
        ans = str(answer or "").strip()
        if not key or not ans or _is_non_answer(ans) or store.is_placeholder(ans) or len(ans) > 120:
            return
        try:
            try:
                ok = config.set_fact(key, ans, create=True)
            except TypeError:
                # config is not hot-reloaded; a server started before `create` existed still writes an
                # existing leaf and leaves a new one for the cache to carry until the next restart.
                ok = config.set_fact(key, ans)
        except Exception as e:  # noqa: BLE001 - the answer is cached either way
            log.debug("could not write %s to facts.yaml: %s", key, e)
            return
        if ok:
            try:
                self.facts = config.load_facts()
            except Exception:  # noqa: BLE001
                pass
            log.info("facts.yaml: %s = %r, from your answer to %r — every later form reads it from there",
                     key, ans[:60], question[:60])

    def knows(self, question: str) -> bool:
        """True when the cache can already answer this, so a value seen on the form teaches nothing new."""
        key = normalize_question(question)
        return bool(key) and self._cache_lookup(key) is not None

    @staticmethod
    def _eligible(rec: AnswerRecord | None, min_trust: int) -> bool:
        """Whether this record may be replayed at all.

        Quarantined and rejected records stay in the file so the candidate can look at them, but they are
        not answers: a value scraped off an untouched dropdown is exactly what must never reach a form
        again. `reusable` is False for a field that belonged to one row of a repeating section.
        """
        return bool(rec) and rec.usable and rec.reusable and rec.trust >= min_trust

    def _cache_lookup(self, key: str, *, min_trust: int = 0, exact_only: bool = False) -> AnswerRecord | None:
        rec = self.records.get(key)
        if self._eligible(rec, min_trust):
            return rec
        if exact_only:
            return None
        best, best_score = None, 0
        for k, r in self.records.items():
            if not self._eligible(r, min_trust):
                continue
            s = fuzz.token_set_ratio(key, k)
            # token_set_ratio scores a strict subset at 100 ("years of experience" vs "years of experience with X"),
            # so also require the sorted-token similarity to be high.
            if s < FUZZY_THRESHOLD or fuzz.token_sort_ratio(key, k) < FUZZY_SORT_THRESHOLD:
                continue
            # Equally close wordings are separated by what they are worth, not by dict order.
            if s > best_score or (s == best_score and best is not None and self._rank(r) > self._rank(best)):
                best, best_score = r, s
        return best

    @staticmethod
    def _rank(rec: AnswerRecord) -> tuple:
        return (rec.trust, rec.confidence == "confirmed", rec.learned_at)

    # ---------- protected / EEO ----------
    @classmethod
    def is_protected(cls, question: str) -> bool:
        return bool(kw_regex(cls.PROTECTED_KEYWORDS).search(normalize_question(question)))

    @classmethod
    def is_eeo(cls, question: str) -> bool:
        return bool(kw_regex(cls.EEO_KEYWORDS).search(normalize_question(question)))

    # ---------- public ----------
    def answer(self, question: str, options: list[str] | None = None, kind: str = "text") -> str:
        """Answer one form question. Remembers what it answered, so an "if yes, explain" that follows can see
        whether its condition was met."""
        ans = self._answer(question, options, kind)
        if kind == "number":
            ans = self._numeric(question, ans)
        self.previous_answer = ans
        return ans

    def _numeric(self, question: str, ans: str) -> str:
        """`ans` as a number for a number box, or a pause so the user can give one.

        Everything upstream answers in prose because that is what most forms want. A number box is the one
        control that cannot take prose at all — the browser keeps the stray "e" out of "Negotiable" and then
        refuses the submit — so the conversion happens here, once, wherever the answer came from.
        """
        if not ans:
            return ""
        num = as_number(ans, normalize_question(question))
        if num is None:
            log.info("%r wants a number and %r is not one; asking", question, ans[:60])
            raise NeedsHuman(f"Needs a number: {question}", question=question, options=[], kind="number")
        if num != ans:
            log.info("numeric field %r: answering %r as %r", question, ans[:60], num)
        return num

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
        # question the form never actually asked. Only for free text and number boxes ("If yes, how many
        # years?") — a conditional dropdown does not exist.
        if kind in ("text", "textarea", "number") and not options:
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
                    self.learn(question, pick, source="rule", kind=kind, options=options)
                    return pick
                # Nothing on this list is true. Ask — never let the model pick one, which is how a form
                # asking where you heard about the job came to say YouTube: the options were Facebook,
                # Instagram, X and YouTube, and a guess among them is a false statement on a real
                # application, not a style choice.
                raise NeedsHuman(f"Needs your answer: {question}", question=question, options=options,
                                 kind=kind)
            else:
                self.learn(question, src, source="rule", kind=kind)
                return src

        # (0.5) Protected questions, decided before the cache is even consulted. This ordering is the whole
        # point: the EEO gate used to sit below the cache, so an "ethnicity" scraped off an untouched
        # dropdown answered a real EEO question, and a plain Yes/No "right to work in Australia" was served
        # from an entry learned on a different continent. What may not be guessed may not be replayed.
        protected = self._protected_answer(key, question, options, kind)
        if protected is not None:
            return protected

        # (a)+(b) cache. A cached placeholder is ignored rather than replayed, so a bad answer from an earlier
        # run self-heals into a real question instead of being pasted onto every future application.
        rec = self._cache_lookup(key)
        cached = rec.answer if rec is not None else None
        if cached is not None and (_is_non_answer(cached) or store.is_placeholder(cached)):
            # Including a list's prompt learned before learn() refused to store one: answers.json is the
            # user's file and is not rewritten behind their back, so the bad entries already in it heal
            # here, by being ignored and asked again.
            cached = None
        # A cached answer the control cannot hold is no answer here, whatever it was worth on the form it
        # was learned from: "Negotiable" is a good answer to a salary box and nothing at all to a salary
        # number box. Ignored rather than replayed, so the run asks once and heals the cache instead of
        # bouncing off the same field on every retry.
        if cached is not None and kind == "number" and as_number(cached, key) is None:
            log.info("ignoring the cached %r for %r: the field takes only a number", cached[:40], question)
            cached = None
        # An answer the model wrote once must not outrank the facts file for ever. When a rule can decide
        # this question and disagrees, the rule wins and the cache heals: otherwise editing facts.yaml has
        # no visible effect, which is how a stale guess survives every later application.
        if cached is not None and rec is not None and rec.trust <= TRUST["llm"]:
            rule = self._rule_answer(key, question)
            if rule is not None and rule != cached:
                log.info("facts now answer %r as %r; dropping the cached %r (%s)",
                         question, rule, cached[:40], rec.source)
                cached = None
        if cached is not None:
            mapped = self._map_answer(key, cached, options)
            if mapped is not None:
                return mapped
            if not options:
                return cached

        # (c) rules
        rule = self._rule_answer(key, question)
        if rule is not None:
            # (d) map to options
            mapped = self._map_answer(key, rule, options)
            if mapped is not None:
                self.learn(question, mapped, source="rule", kind=kind, options=options)
                return mapped
            if not options:
                self.learn(question, rule, source="rule", kind=kind)
                return rule
            # rule produced a value that isn't among the offered options
            if self.is_protected(question):
                raise NeedsHuman(f"Could not map answer {rule!r} to the options for: {question}",
                                 question=question, options=options, kind=kind)

        # (e) LLM fallback. Work authorisation and EEO stay facts-only: those answers are legal declarations
        # and a wrong one is a false statement on a real application, not a style slip. _protected_answer
        # has already settled or stopped those above; this stays as the backstop, so that widening a keyword
        # set can never quietly open one of them to the model.
        if self.is_protected(question) or self.is_never_guess(question):
            raise NeedsHuman(f"Needs your answer (protected question): {question}",
                             question=question, options=options, kind=kind)
        if self.llm_enabled:
            llm = self._llm_answer(question, options)
            if llm is not None:
                self.learn(question, llm, source="llm", kind=kind, options=options)
                return llm
        raise NeedsHuman(f"Needs your answer: {question}", question=question, options=options, kind=kind)

    # ---------- protected questions, decided before the cache ----------
    def _protected_answer(self, key: str, question: str, options: list[str], kind: str) -> str | None:
        """The answer a protected question is allowed to have, or None when the question is not protected.

        This never falls through to the generic cache, the model, or NeedsHuman's caller: a protected
        question either gets an answer it is entitled to or it stops the run. The two classes are not the
        same thing, and the difference is where the answer may come from:

          FACTS_ONLY          scoped to a country or a law (visa, citizenship, EEO, a contact detail).
                              facts.yaml decides; a remembered answer is only allowed if the candidate
                              themselves gave it, and then only on the very same wording.
          REUSABLE_PROTECTED  a figure only the candidate sets but which does not change with the employer
                              (salary, day rate). Their own answer first, then the facts file — that way
                              round because the salary rule ends in a "Negotiable" fallback, which is a
                              default and not something they ever said.
        """
        # A consent gate is the price of submitting, not a fact about the candidate, and is_agreeable already
        # keeps claims of fact out: "Consent to collect, use and disclose your personal data ... individuals
        # with appropriate qualifications" was stopped here on the word "qualifications" (application 277).
        facts_only = is_facts_only(key) and not is_agreeable(key)
        if not facts_only and not is_reusable_protected(key):
            return None
        stopped = f"Needs your answer (protected question): {question}"

        if not facts_only:
            picked = self._remembered_protected(key, options, kind, min_trust=TRUST["typed"])
            if picked is not None:
                return picked
            picked = self._rule_for_protected(key, question, options, kind)
            if picked is not None:
                return picked
            raise NeedsHuman(stopped, question=question, options=options, kind=kind)

        # Workday's two-level "select the country where the job is located and indicate your eligibility":
        # the first level is a country list, and it is the job's country, not the candidate's -- AIA's form
        # was searched for "Bangladesh" (application 283). The second level, the eligibility sentences,
        # falls through to the authorisation rules below.
        if _JOB_COUNTRY_Q_RE.search(key):
            country = self._job_country()
            if country:
                if not options:
                    return country
                picked = self._map_to_options(country, options)
                if picked is not None:
                    return picked

        # Work authorisation depends on where the job is, so an answer cached at one employer is wrong at
        # the next: "Yes, I am currently authorized to work; I will need sponsorship in future" was learned
        # on a Bangladesh-scoped form and replayed onto an Australian one. The facts file decides these.
        if _AUTH_QUESTION_RE.search(key):
            picked = self._authorization_answer(key, options)
            if picked is not None:
                return picked
            if (not options and kind in ("text", "textarea") and _AUTH_OPEN_QUESTION_RE.search(key)
                    and not (_YES_NO_SHAPE_RE.match(key) and not _ASKS_FOR_DETAIL_RE.search(key))):
                stated = self._authorization_statement(key)
                if stated:
                    log.info("answering %r from the authorization facts: %r", question, stated)
                    return stated
            # A plain Yes/No pair, which _authorization_answer declines because the rules already know the
            # answer. It used to fall through to the cache from here, and that is how a "No" learned about
            # Australia came to answer the same question about anywhere else. Ask the facts instead.
            picked = self._rule_for_protected(key, question, options, kind)
            if picked is not None:
                return picked

        # EEO: answered only from the eeo block in facts.yaml, which the candidate filled in themselves.
        # Never guessed by the model, and never auto-declined — picking "I prefer not to answer" would still
        # be deciding for them. A value they have not given means the run stops and asks.
        elif self.is_eeo(question):
            stopped = f"Your answer needed: {question}"
            stated = self._eeo_answer(key)
            if stated:
                mapped = self._map_eeo_to_options(key, stated, options) if options else stated
                if mapped is not None:
                    self.learn(question, mapped, source="rule", kind=kind, options=options)
                    return mapped
                log.info("EEO answer %r does not match any option for %r; asking", stated, question)

        else:
            # Everything else the keywords catch: a contact detail, a date of birth, a clearance. These
            # match incidentally more often than the two blocks above — "Verification code from the email"
            # contains "email" — so what the candidate actually said about this exact question comes first,
            # before a rule derived from a keyword answers a different question entirely.
            picked = self._remembered_protected(key, options, kind, min_trust=TRUST["human"], exact_only=True)
            if picked is None:
                picked = self._rule_for_protected(key, question, options, kind)
            if picked is not None:
                return picked

        # Their own answer to this very question, as a last resort before stopping. Exact wording only and
        # nothing below a human answer: it is behind the facts rather than in front of them because a visa
        # answer learned at one employer is a false statement at the next, but it is still ahead of giving
        # up when the facts file simply has nothing to say.
        picked = self._remembered_protected(key, options, kind, min_trust=TRUST["human"], exact_only=True)
        if picked is not None:
            return picked
        raise NeedsHuman(stopped, question=question, options=options, kind=kind)

    def _rule_for_protected(self, key: str, question: str, options: list[str], kind: str) -> str | None:
        rule = self._rule_answer(key, question)
        if rule is None:
            return None
        mapped = self._map_answer(key, rule, options) if options else rule
        if mapped is None:
            return None
        self.learn(question, mapped, source="rule", kind=kind, options=options)
        return mapped

    def _remembered_protected(self, key: str, options: list[str], kind: str, *, min_trust: int,
                              exact_only: bool = False) -> str | None:
        rec = self._cache_lookup(key, min_trust=min_trust, exact_only=exact_only)
        if rec is None or _is_non_answer(rec.answer):
            return None
        if kind == "number" and as_number(rec.answer, key) is None:
            return None
        return self._map_answer(key, rec.answer, options) if options else rec.answer

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
            # "No, I will require sponsorship to work in this country" (AIA's Workday, application 283): the
            # "No" answers "are you eligible", it does not negate the sponsorship, so it is not read as one.
            body = re.sub(r"^\s*(?:yes|no)\b[\s,:-]*", " ", low.strip())
            sponsor_negated = bool(re.search(r"\b(?:not|no|never|without)\b[^.]{0,40}sponsor|sponsor[^.]{0,30}\b(?:not|no)\b"
                                             r"|will not require|do not require|don t require|dont require|not need", body))
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
            # A status picker ("Citizen / Visa Holder / Permanent Resident / Other", Heidi on Ashby,
            # application 199) names statuses rather than making claims in sentences. Where the candidate is
            # not authorised, holding a visa there is as false as citizenship, and the catch-all is the one
            # true pick -- without these two terms every option scored 0 or less and the run stopped to ask.
            if authorized_now is False and not says_sponsor:
                if re.search(r"\b(?:visa holder|work visa|work permit|temporary resident|holds? a visa)\b", low):
                    score -= 2
                elif re.fullmatch(r"\s*(?:other|none|none of the above|neither|not applicable|n a)\s*", low):
                    score += 1
            scored.append((score, o))
        if not scored:
            return None
        scored.sort(key=lambda t: -t[0])
        best, best_score = scored[0][1], scored[0][0]
        if best_score <= 0 or (len(scored) > 1 and scored[1][0] == best_score):
            return None       # nothing on the list clearly states the truth: ask
        self.learn(key, best, source="rule", options=options)
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
        # A list scoped to the employer's country -- "Chinese (Thailand) / Others (Thailand) / Thai
        # (Thailand)" (Lumentum, application 285) -- names no group the stated "Asian" could be; its own
        # "Others" is then the one true entry. Only when nothing above matched, and only for ethnicity.
        if "ethnic" in key or "race" in key:
            for o in options:
                if _OTHER_OPTION_RE.match(re.sub(r"\s*\([^)]*\)\s*$", "", str(o).strip())):
                    return o
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
        if has("military status", "military service"):
            # Thai boards ask this of everyone ("Exempted / Conscripted / No military service"): a question
            # about national service, not about US protected-veteran status, so it has a fact of its own.
            return str(eeo.get("military_service") or eeo.get("veteran_status") or "").strip()
        if has("veteran", "armed forces", "protected veteran"):
            return str(eeo.get("veteran_status") or "").strip()
        if has("disabilit", "disabled"):
            return str(eeo.get("disability_status") or "").strip()
        if has("religion", "religious", "faith"):
            return str(eeo.get("religion") or "").strip()
        if has("race", "ethnic", "hispanic", "latino"):
            return str(eeo.get("race_ethnicity") or "").strip()
        # Pronouns are their own fact, never read off gender: "Male" is not an option on a pronoun list
        # (application 172, Deloitte NZ: "Could not choose 'Male' for 'What are your pronouns?'"), and
        # which pronouns someone uses is theirs to say. Blank means the run asks once.
        if has("pronoun ", "pronouns "):
            return str(eeo.get("pronouns") or "").strip()
        if has("gender", "sex "):
            return str(eeo.get("gender") or "").strip()
        return ""

    # ---------- option mapping ----------
    def _alternatives_for(self, key: str) -> list[str]:
        """Other wordings of an answer that facts.yaml has already said are acceptable.

        Only the field of study has them, and only because a degree subject is the one fact whose exact
        wording differs from board to board: the certificate says "Computer Science & Engineering" and a
        board's list offers "Computer Science". Naming the substitutes in facts.yaml keeps the decision the
        candidate's rather than a fuzzy match's.
        """
        if _FIELD_OF_STUDY_KEY_RE.search(key or ""):
            alts = self.fact("education.field_of_study_alternatives", []) or []
        elif _DEGREE_LEVEL_KEY_RE.search(key or "") and "classification" not in (key or ""):
            # The degree too: "Bachelor's Degree" is on no list that spells out "Bachelor of Science (B.S)"
            # beside "Bachelor of Engineering" and "Bachelor of Arts (B.A)" (Lumentum's Workday, application 285).
            alts = self.fact("education.degree_alternatives", []) or []
        else:
            return []
        return [str(a).strip() for a in alts if str(a).strip()]

    def _map_answer(self, key: str, value: str, options: list[str]) -> str | None:
        """`value` on this form's option list, falling back to the wordings facts.yaml allows instead.

        Without this a truthful answer that the employer's list does not word the same way stopped the run:
        "Could not map answer 'Computer Science & Engineering' to the options" paused an application whose
        list offered "Computer Science" — which facts.yaml had already named as acceptable, and which the
        suggestion-box path was already using.
        """
        mapped = self._map_to_options(value, options)
        if mapped is not None:
            return mapped
        for alt in self._alternatives_for(key):
            mapped = self._map_to_options(alt, options)
            if mapped is not None:
                log.info("%r is not on this form's list; taking %r, which facts.yaml allows instead",
                         value, mapped)
                return mapped
        if _OTHER_OK_KEY_RE.search(key or "") or _FIELD_OF_STUDY_KEY_RE.search(key or ""):
            other = next((o for o in options if _OTHER_OPTION_RE.match(str(o).strip())), None)
            if other is not None:
                log.info("%r is not on this form's list; taking its %r", value, other)
                return other
        return None

    @staticmethod
    def _map_to_options(value: str, options: list[str]) -> str | None:
        # The list's own prompt is not something an answer may map onto, however exactly it matches.
        options = store.real_options(options)
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
        # A list that tags every entry with the employer's country: "Muslim (Thailand)", "Others (Thailand)".
        for o in options:
            if normalize_question(re.sub(r"\s*\([^)]*\)\s*$", "", str(o))) == nv:
                return o
        # A numbered list ("5 - Bachelors") and a degree named by its level: AGF's Workday "Degree" list did
        # not match "Bachelor's Degree" and stopped to ask (application 251).
        unnum = {re.sub(r"^\s*\d+\s*[-.):]?\s*", "", normalize_question(o)): o for o in options}
        if nv in unnum:
            return unnum[nv]
        level = re.search(r"\b(bachelor|master|doctor|phd|associate|diploma)", nv)
        if level:
            hits = [o for n, o in unnum.items() if re.search(rf"\b{level.group(1)}", n)]
            if len(hits) == 1:
                return hits[0]
        # The same thing under the other names lists give it ("Mobile" is "Cell" on some Workday tenants).
        for alias in _OPTION_ALIASES.get(nv, ()):
            for o in options:
                if normalize_question(o) == alias:
                    return o
        # fuzzy option match (e.g. "5" vs "5+ years", "Bangladesh" vs "Bangladesh (BD)")
        best, best_score = None, 0
        for o in options:
            no = normalize_question(o)
            s = fuzz.token_set_ratio(nv, no)
            # token_set_ratio scores a subset as a perfect match, which "Bangladesh (BD)" and "5+ years"
            # need -- and which landed "Engineering" on "Aerospace Engineering" (Lumentum's Workday,
            # application 285). Extra words may only trail the answer as noise, never change its meaning.
            if s >= 95 and nv != no and not (_extends_cleanly(nv, no) or _extends_cleanly(no, nv)
                                             or fuzz.ratio(nv, no) >= 90):
                continue
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

        # --- the job's country, not the candidate's ---
        # "Select the name of the country where the job is located..." (AIA's Workday, application 283) fell
        # to the identity rule below and was searched for Bangladesh. Ahead of everything else on purpose.
        if _JOB_COUNTRY_Q_RE.search(key):
            country = self._job_country()
            if country:
                return country

        # --- legal age ---
        # "Are you over the age of 18?" -- facts.yaml keeps no date of birth, only identity.over_18 (derived from
        # the education dates). AGF's Workday stopped to ask it (application 251).
        if re.search(r"\b(?:over|at least|older than)\s+(?:the\s+)?(?:age\s+of\s+)?(?:18|eighteen|21|twenty one)\b"
                     r"|\b(?:18|21)\s+years?\s+(?:of\s+age|old)\s+or\s+older\b|\blegal\s+(?:working\s+)?age\b", key):
            over = self.fact("identity.over_18", None)
            if over is not None:
                return "Yes" if over else "No"

        # --- the date of signing ---
        # "Today's date" beside a signature box. The model answered "2026-02-01" on 2026-09-29 and the cache
        # kept it for every later form (application 222, ServiceNow): a date is a fact of the day, never a
        # guess and never a memory. ISO, which date boxes and free text both take.
        if re.fullmatch(r"(?:today s|todays|today|current|signature|signing|submission|application)\s+date"
                        r"|date(?:\s+(?:of\s+)?(?:signature|signing|today|submission|application))?|date signed"
                        r"|date \(?(?:dd mm yyyy|mm dd yyyy|yyyy mm dd)\)?", key):
            from datetime import date
            return date.today().isoformat()

        # --- consent boxes ("I accept", "I have read the privacy notice", "I certify the above is true") ---
        # Ticking these is the price of applying at all; there is no honest "No" that still submits. Since
        # 2026-09-15 the optional opt-ins beside them are taken too, at the user's instruction, so that
        # nothing stops on a tickbox. Ahead of every other rule, and is_agreeable keeps claims of fact out.
        if is_agreeable(key):
            return "Yes"

        # "Are you a citizen or permanent resident of Cuba, Syria, Iran…? (…export control authorizations)" --
        # an export-control screen, a yes/no about a list of countries. It fell to the "authoriz" rule below
        # on the word in its footnote, found no country there, and ServiceNow's form stopped to ask
        # (application 222). Yes only when the list names the citizenship or the country lived in.
        cit = str(auth.get("citizenship") or "").strip()
        # Any wording of it: Telnyx's "OFAC: Please indicate whether you are either a citizen or lawful
        # permanent resident of any of the following countries: Cuba, Crimea, Iran…" (application 241).
        _sanctions = re.findall(r"\b(?:cuba|syria|iran|north korea|crimea|sudan|russia|belarus|venezuela|donetsk|luhansk)\b", key)
        if cit and ((re.match(r"^(?:are|is)\s+(?:you|the candidate)\s+(?:a\s+)?(?:citizen|national|(?:lawful\s+)?permanent\s+resident)\b", key))
                    or (has("citizen", "permanent resident") and len(set(_sanctions)) >= 2)):
            mine = [x.lower() for x in (cit, f("identity.country")) if x]
            if any(re.search(rf"\b{re.escape(m)}\b", key) for m in mine):
                return "Yes"
            if re.search(r"\b(?:of|from|in)\b", key) and re.search(
                    r"\b(?:cuba|syria|iran|north korea|crimea|sudan|russia|belarus|venezuela|united states|u s|us|usa"
                    r"|canada|australia|singapore|united kingdom|uk|eu|european union|new zealand|india|pakistan|china"
                    r"|germany|france|netherlands|ireland|japan|uae|united arab emirates|saudi arabia|qatar)\b", key):
                return "No"

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
        # "Do you require a visa to work in the location you are applying to?" (AIA's Workday, application
        # 283): "visa" makes the question protected, and nothing here answered it. The candidate's side of the
        # sponsorship fact: no visa where they may already work, one everywhere else.
        if has("visa") and has("require", "need"):
            here = self._authorized_here(key)
            if here is not None:
                return "No" if here else "Yes"
        if has("authoriz", "authoris", "legally", "right to work", "eligible to work", "work permit", "permitted to work"):
            countries = [str(c).lower() for c in (auth.get("authorized_countries") or [])]
            # A country named anywhere, not only after "in"/"for": "authorized under UK laws to work for Janus
            # Henderson" put the employer after "for" and the country before it (application 239).
            named = re.search(r"\b(?:under|by)\s+(?:the\s+)?(uk|u k|us|u s|usa|eu|[a-z]+(?: [a-z]+)?)\s+(?:law|laws|legislation)\b"
                              r"|\b(?:in|within)\s+(?:the\s+)?(united kingdom|united states|uk|usa|australia|canada|singapore"
                              r"|new zealand|ireland|germany|netherlands|united arab emirates|uae)\b", key)
            if named:
                country = (named.group(1) or named.group(2) or "").strip()
                aliases = {"uk": "united kingdom", "u k": "united kingdom", "us": "united states", "u s": "united states",
                           "usa": "united states", "uae": "united arab emirates"}
                country = aliases.get(country, country)
                if country in {"united kingdom", "united states", "australia", "canada", "singapore", "new zealand",
                               "ireland", "germany", "netherlands", "united arab emirates", "eu", "bangladesh"}:
                    return "Yes" if any(aliases.get(c, c) == country for c in countries) else "No"
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
        # "…eligible for Security Clearance, meaning you have lived in the UK for the past 5 years
        # continuously. Can you confirm this applies to you?" -- a residency test, and facts.yaml says where the
        # candidate lives. Faculty stopped to ask it (application 236). Only ever a No from here: living in the
        # named country now does not prove the years, so that case still asks.
        m = re.search(r"\blived\s+in\s+(?:the\s+)?([a-z][a-z .]{1,30}?)\s+(?:for|continuously|during|over)\b", key)
        if m:
            named = m.group(1).strip()
            aliases = {"uk": "united kingdom", "u k": "united kingdom", "us": "united states", "usa": "united states",
                       "u s": "united states", "uae": "united arab emirates"}
            named = aliases.get(named, named)
            home = f("identity.country").lower()
            if home and named and named not in home and home not in named:
                return "No"
        if has("clearance"):
            return None

        # --- identity ---
        if has("first name", "given name", "forename"):
            return f("identity.first_name") or None
        if has("last name", "surname", "family name"):
            return f("identity.last_name") or None
        if has("pronounce", "pronunciation", "phonetic"):
            # asks how to say the name, not what it is; facts.yaml if the candidate has said, else the model
            return f("identity.name_pronunciation") or None
        if has("full name", "legal name", "your name", "preferred name") or key in ("name", "candidate name"):
            return f("identity.full_name") or (f("identity.first_name") + " " + f("identity.last_name")).strip() or None
        # The kind of a contact detail, not the detail: Workday's "Phone Device Type" (Landline / Mobile) got
        # the phone number itself from the rule below and stopped the run on "Could not map answer
        # '+880…'" (application 219, Red Hat). The number in facts.yaml is a mobile; the address is personal.
        # "Phone number (without country code)" asks for the number itself, national part only -- the words
        # "country code" made it read as a question about the field, and it stopped to ask (application 224).
        if has("phone", "mobile", "telephone", "contact number") and re.search(
                r"\b(?:without|excluding|excl|no|minus)\s+(?:the\s+)?(?:country|dial(?:l?ing)?|international)\s*(?:code|prefix)", key):
            phone = f("identity.phone")
            dial = _DIAL_CODES.get(f("identity.country").lower(), "")
            national = re.sub(r"\D", "", phone)
            if dial and national.startswith(dial):
                return national[len(dial):].lstrip("0") or None
            return None
        if store.is_about_the_field(key):
            if re.search(r"device\s*type|(?:phone|number)\s*type|type\s+of\s+(?:phone|number)", key):
                return "Mobile"
            if re.search(r"(?:email|address)\s*type|type\s+of\s+(?:email|address)", key):
                return "Personal"
            return None
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
        # "Residency status" is not a place. The "reside" prefix below catches it, and answering it with a
        # home address puts "Dhaka, Bangladesh" in a box asking whether the candidate may live and work in
        # the country — a different question, and a false answer to it. "Country of residence" is untouched.
        if _RESIDENCY_STATUS_RE.search(key):
            return None
        if has("postcode", "post code", "postal code", "zip code", " zip "):
            return f("address.postcode") or None
        if has("time zone", "timezone"):
            return f("identity.time_zone") or None
        if key in ("city", "town", "suburb") and f("address.city"):
            return f("address.city")
        if key in ("state", "province", "region", "county") and f("address.state"):
            return f("address.state")
        if has("street address", "address line 1") or key == "street":
            return f("address.street") or None
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

        # --- education ---
        # Order matters here and is not alphabetical. "Field of Degree" and "Degree discipline" both carry
        # the word "degree" while asking what was studied, so the subject rule has to be tested before the
        # qualification rule or both land on "Bachelor's Degree". Likewise "graduate school" is a school.
        #
        # These come from facts.yaml alone (see answers.EDUCATION_KEYWORDS). The model used to answer them
        # from the CV and the cache used to remember whatever a form's own list was resting on, which is
        # how "Accounting" became the stored field of study on 2026-09-20 and was then offered to every
        # employer after it, and how a Degree box came to hold "Computer Science & Engineering" — the
        # subject, in the box asking for the qualification.
        if has("field of study", "field of degree", "discipline", "major", "course of study",
               "subject of study", "area of study", "specialisation", "specialization", "concentration"):
            return f("education.field_of_study") or None
        if has("school", "university", "college", "institution", "alma mater", "educational establishment"):
            return f("education.school") or None
        # "Do you hold a STEM degree?" is a yes/no about the degree, not a request for its name: answered
        # "Bachelor's Degree", nothing on a Yes/No list matched and Mistral's form stopped to ask (app 217).
        # Settled from the education block alone: a higher degree than the one held is a No, a degree of the
        # kind held (bachelor's, STEM, computing, engineering, any university degree) a Yes.
        if re.match(r"^(?:do|does|have|are)\s+you\s+(?:currently\s+)?(?:hold|have|possess|obtained|completed|earned|got)\b"
                    r"|^(?:is|was)\s+your\s+degree\b", key) and "degree" in key:
            held = (f("education.degree") + " " + f("education.field_of_study")).lower()
            if not held.strip():
                return None
            higher = re.search(r"\b(?:master|msc|m sc|mba|phd|ph d|doctor|doctoral|postgraduate|graduate degree)\b", key)
            if higher:
                return "Yes" if re.search(r"master|phd|doctor|mba", held) else "No"
            if re.search(r"\b(?:stem|computer|computing|engineering|technical|science|bachelor|undergraduate"
                         r"|university|college|tertiary|relevant|related|4 year|four year)\b", key):
                stem = bool(re.search(r"computer|engineering|science|math|physics|technology", held))
                return "Yes" if (stem or not re.search(r"\b(?:stem|computer|computing|engineering|technical|science)\b", key)) else None
            return None
        # The transcript: a grade, its classification, and when the course ran. None of these is in the CV
        # and none may be guessed, so facts.yaml alone answers them (education.gpa was given by the candidate
        # on application 274 and written there through _remember_fact).
        if has("gpa", "cgpa", "grade point", "cumulative grade"):
            gpa = f("education.gpa")
            if not gpa:
                return None
            scale = f("education.gpa_scale")
            if scale and "/" not in gpa and re.search(r"\bout of\b|/|\bscale\b", key):
                return f"{gpa}/{scale}"
            return gpa
        if has("classification", "honours", "honors", "class of degree"):
            return f("education.degree_classification") or None
        # "Course Period — Course Start Month", "Education start date", "Expected graduation date": the
        # education block's months and years. Gated on an education word so an availability "start date"
        # or an employment row's "end date" never lands here.
        if (has("course", "study", "studies", "education", "school", "university", "college", "degree",
                "academic", "graduat", "enrol")
                and has("period", " start", " end", " from ", " to ", " date", " month", " year", "graduat", "complet")):
            start = has(" start", " from ", " begin", " commence")
            end = has(" end", " to ", "graduat", "complet", " finish", " until")
            if start and not end:
                return " ".join(x for x in (f("education.start_month"), f("education.start_year")) if x) or None
            if end:
                return " ".join(x for x in (f("education.end_month"), f("education.end_year")) if x) or None
        if has("degree", "qualification", "education level", "level of education", "highest education",
               "highest level"):
            return f("education.degree") or None

        # --- religion (Thai boards ask it; answered only from facts.yaml) ---
        if has("religion", "religious"):
            rel = str((self.fact("eeo", {}) or {}).get("religion") or "").strip()
            if rel:
                return rel

        # --- languages ---
        # "What is your level of proficiency in Thai?" (SKY ICT, application 281). Only a language the facts
        # name is answered; one they do not is asked, because these lists offer no honest "none".
        if has("proficien", "fluen", "language"):
            for name, level in (self.fact("languages", {}) or {}).items():
                if re.search(rf"\b{re.escape(str(name).lower())}\b", key):
                    return str(level)

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
        if not options and re.fullmatch(r"(?:select resume\s+)?[^\n/\\]{1,150}\.(?:pdf|docx?|rtf|txt|odt)", reply, re.I):
            # A file name is never an answer typed into a question. It is what the model produces when the
            # "question" was an upload widget's own text (application 175: "Drop or select (.doc / .pdf)").
            log.info("LLM answered %r with a file name; not using it", question[:60])
            return None
        if options:
            return self._map_answer(normalize_question(question), reply, options)
        return reply


def _adopt_live_resolvers() -> int:
    """Move every Resolver built from an older copy of this module onto the class just loaded.

    A reload replaces the module's functions, but a paused application keeps the Resolver instance it was
    started with, and that instance's methods are still the old ones. runner._refresh_adapter re-reads
    facts.yaml into it and reloads this module, yet the retry went on answering with the old rules
    (application 172: the pronoun rule was fixed, reloaded, and "Male" was still chosen for "What are your
    pronouns?"). Rebinding the class is the whole fix: the instance's state stays, its behaviour is new.
    """
    import gc

    moved = 0
    for obj in gc.get_objects():
        cls = type(obj)
        if cls is not Resolver and cls.__name__ == "Resolver" and cls.__module__ == __name__:
            try:
                obj.__class__ = Resolver
                moved += 1
            except TypeError:       # a layout the new class cannot take; leave it on the old code
                continue
            _refresh_records(obj)
    if moved:
        log.info("resolver: %d running application(s) now answer with the reloaded rules", moved)
    return moved


def _refresh_records(resolver: "Resolver") -> None:
    """Bring a paused application's answer memory up to date with answers.json as it is on disk now.

    The runner re-reads facts.yaml for a retry (an edit made while the window waited must land), but the
    resolver's records were read once at the start of the run. An entry corrected in Settings — or a
    record this very run wrongly marked rejected, as application 274 did to the CGPA the candidate had just
    typed — therefore stayed as it was in memory, and the retry asked the question again. Newer wins, so a
    one-time code learned this run (never written to disk) is kept.
    """
    try:
        disk = config.load_answer_records()
    except Exception as e:  # noqa: BLE001
        log.debug("resolver: could not re-read answers.json: %s", e)
        return
    changed = 0
    for key, rec in disk.items():
        mine = resolver.records.get(key)
        if mine is None or (rec.learned_at or "") >= (mine.learned_at or ""):
            if mine is None or rec.to_json() != mine.to_json():
                changed += 1
            resolver.records[key] = rec
    resolver.answers = store.text_view(resolver.records)
    if changed:
        log.info("resolver: %d answer record(s) refreshed from answers.json for the retry", changed)


_adopt_live_resolvers()
