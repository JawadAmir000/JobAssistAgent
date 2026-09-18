"""Write a cover letter for one job, grounded in the CV.

One letter per job, cached in the database so a re-run or a resume reuses the same text rather than paying
for a new one and sending a different letter to the same employer.

Everything in the letter has to come from the CV and facts.yaml. The letter is the candidate's own claim
about their experience, so an invented employer or number is not a style problem, it is a false statement on
a real application.

Contract:
    letter_for(job, cv_text, facts) -> str      # '' when there is no LLM configured
"""
from __future__ import annotations

import logging
import pathlib
import re

from jobbot import db

log = logging.getLogger(__name__)

MAX_WORDS = 260
MAX_JD_CHARS = 3500      # enough for the responsibilities and requirements; the rest is boilerplate and perks

# Phrases that make a letter read like it came out of a machine. Checked after generation, not just asked for
# in the prompt: the model complies less reliably than a regex does.
_AI_TELLS = re.compile(
    r"\bas an ai\b|\bi am excited to leverage\b|\bleverage my passion\b|\bdelve\b|\btapestry\b"
    r"|\bin today'?s (?:fast[- ]paced|ever[- ]changing|rapidly evolving)\b|\bi am writing to express my "
    r"(?:strong )?interest\b|\bperfect (?:fit|candidate) for\b|\bi believe i would be a(?:n)? (?:great|"
    r"perfect|excellent)\b|\bhereby\b|\besteemed\b|\bsynergy\b|\bgame[- ]chang", re.I)

_PROMPT = """Write a cover letter for this job application, as the candidate, in their voice.

Candidate CV:
{cv}

Facts: {facts}

Company: {company}
Role: {title}
{jd}
Rules:
- Ground every claim in the CV. Never invent an employer, a date, a title, a degree, a certification or a
  number that is not there. If the job wants something the candidate has not done, do not claim it.
- Open with why this company and this role specifically, using something real from the posting. Never open
  with "I am writing to express my interest".
- Two or three short paragraphs, under {max_words} words, no headings, no bullet points.
- Plain first-person English, the way a competent engineer writes to another person. Short sentences.
  Concrete specifics over adjectives. No corporate filler, no "passionate", no "leverage", no "synergy",
  no em-dashes, no three-item lists of adjectives, no closing sales pitch.
- Do not restate the whole CV. Pick the two things most relevant to this role and say what was actually
  built and what it did.
- Do not include a date, an address block, or a subject line. Start at the greeting or the first sentence.
- Sign off with the candidate's name and nothing else.

Return only the letter text."""


def _fact(facts: dict, path: str, default: str = "") -> str:
    cur = facts or {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return "" if cur is None else str(cur)


def cached(job_id: str) -> str:
    try:
        return db.get_cover_letter(job_id) or ""
    except Exception as e:  # pragma: no cover - a cache miss must never break an application
        log.debug("cover letter cache read failed: %s", e)
        return ""


def looks_machine_written(text: str) -> bool:
    return bool(_AI_TELLS.search(text or ""))


def letter_for(job: dict, cv_text: str, facts: dict, *, force: bool = False) -> str:
    """The cover letter for this job. Cached per job; '' when no LLM is configured or generation failed."""
    job_id = str(job.get("id") or "")
    if job_id and not force:
        existing = cached(job_id)
        if existing:
            return existing

    if not (cv_text or "").strip():
        log.warning("no CV text, skipping the cover letter")
        return ""

    jd = (job.get("description") or "").strip()
    jd = f"Job description:\n{jd[:MAX_JD_CHARS]}\n" if jd else ""
    compact = {
        "name": _fact(facts, "identity.full_name"),
        "current_title": _fact(facts, "work.current_title"),
        "current_company": _fact(facts, "work.current_company"),
        "years_experience": _fact(facts, "work.years_experience"),
        "location": _fact(facts, "identity.location"),
        "skills": (facts or {}).get("skills", []),
    }
    prompt = _PROMPT.format(cv=cv_text[:12000], facts=compact, company=job.get("company", ""),
                            title=job.get("title", ""), jd=jd, max_words=MAX_WORDS)

    try:
        from jobbot.llm import complete
    except Exception as e:  # pragma: no cover
        log.warning("llm import failed, no cover letter: %s", e)
        return ""

    text = ""
    for attempt in (1, 2):
        try:
            reply = complete(prompt if attempt == 1 else prompt + _RETRY_NOTE,
                             purpose="cover_letter", job_id=job_id or None, max_tokens=700)
        except Exception as e:
            log.warning("cover letter generation failed: %s", e)
            return ""
        text = _clean(reply)
        if not text:
            return ""
        if not looks_machine_written(text):
            break
        log.info("cover letter read as boilerplate, rewriting (attempt %d)", attempt)

    if job_id:
        try:
            db.set_cover_letter(job_id, text)
        except Exception as e:  # pragma: no cover
            log.debug("cover letter cache write failed: %s", e)
    return text


_RETRY_NOTE = ("\n\nThe previous attempt used stock cover-letter phrasing. Rewrite it so it reads like a "
               "person wrote it: no 'I am writing to express my interest', no 'passionate', no 'leverage', "
               "no 'perfect fit'. Say what they built and why this role.")


def _clean(reply: str) -> str:
    """Strip the wrapper a model tends to add (a preamble, code fences, a subject line)."""
    text = (reply or "").strip()
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    text = re.sub(r"^(here(?:'s| is) (?:the|a|your)[^\n:]*:|cover letter:?)\s*\n+", "", text, flags=re.I)
    text = re.sub(r"^(subject|re|date):.*\n+", "", text, flags=re.I | re.M)
    # Em-dashes are the single clearest tell that a model wrote this; the prompt asks for none and the model
    # still slips one in. Rewrite rather than regenerate: the sentence is fine, the punctuation is not.
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    # Collapse runs of blank lines but keep paragraph breaks.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- the letter as a file ----------
def letter_file(text: str, name: str, accept: str = "") -> pathlib.Path:
    """Write the letter to a temp file the form will take: a PDF unless the input's `accept` list rules
    PDFs out and allows plain text. Returns the path."""
    import tempfile
    acc = (accept or "").lower()
    ext = ".txt" if (acc and "pdf" not in acc and ("txt" in acc or "text/plain" in acc)) else ".pdf"
    path = pathlib.Path(tempfile.gettempdir()) / f"{name}-cover-letter{ext}"
    if ext == ".txt":
        path.write_text(text, encoding="utf-8")
    else:
        text_to_pdf(text, path)
    return path


def text_to_pdf(text: str, path: pathlib.Path, width_chars: int = 92, lines_per_page: int = 46) -> None:
    """A plain single-font PDF of `text`, written without a PDF library.

    Helvetica 11pt with WinAnsi encoding: characters outside it are replaced, which a cover letter in
    English never hits. Hand-rolled because it is forty lines and spares a dependency for one attachment.
    """
    import textwrap
    lines: list[str] = []
    for para in (text or "").replace("\r", "").split("\n"):
        lines.extend(textwrap.wrap(para, width_chars) or [""])
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]

    def pdf_str(s: str) -> bytes:
        b = s.encode("cp1252", "replace")
        return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",     # the page tree, filled in once the pages exist
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    page_refs: list[int] = []
    for pg in pages:
        content = (b"BT /F1 11 Tf 15 TL 60 780 Td\n"
                   + b"".join(b"(" + pdf_str(line) + b") Tj T*\n" for line in pg) + b"ET")
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        content_num = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                       b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_num)
        page_refs.append(len(objects))
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % r for r in page_refs)
                  + b"] /Count %d >>" % len(page_refs))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for num, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
