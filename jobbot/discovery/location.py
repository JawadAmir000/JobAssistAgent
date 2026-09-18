"""Location matching: does a posting's location string satisfy the user's wanted locations?

    matches(job_location, job_remote, wanted) -> bool
    filter_jobs(jobs, wanted, max_age_days) -> (kept, dropped_reasons)
    country_for(location) -> jobspy `country_indeed` string
    is_fresh(posted_at, found_at, max_age_days) -> bool

Job boards answer "Canada" with "Toronto, Ontario", "London" with no country at all, and Lever
packs several cities into one string. So a wanted term expands to the country's names, ISO code,
regions and big cities, and a posting matches when any of those appears in its location.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

__all__ = ["matches", "filter_jobs", "country_for", "is_fresh", "countries_named", "REMOTE_WORDS"]

# Words that mean "no office" rather than a place.
REMOTE_WORDS = ("remote", "anywhere", "distributed", "worldwide", "work from home", "wfh",
                "virtual", "telecommute", "home based", "fully remote")

# canonical country -> (jobspy country_indeed, name aliases, uppercase codes, regions/cities)
# Codes are only ever matched as standalone uppercase tokens, so "OR"/"IN" can't hit prose.
_C: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "united states": ("usa",
        ("united states", "united states of america", "usa", "u s a", "america"),
        ("US", "USA"),
        ("california", "new york", "texas", "washington state", "massachusetts", "illinois",
         "colorado", "georgia", "florida", "virginia", "maryland", "oregon", "utah", "arizona",
         "san francisco", "sf bay area", "bay area", "silicon valley", "palo alto", "mountain view",
         "menlo park", "sunnyvale", "santa clara", "san jose", "oakland", "berkeley", "los angeles",
         "san diego", "santa monica", "culver city", "costa mesa", "irvine", "new york city", "nyc",
         "manhattan", "brooklyn", "boston", "cambridge ma", "somerville", "seattle", "bellevue",
         "redmond", "austin", "dallas", "houston", "denver", "boulder", "chicago", "atlanta",
         "miami", "philadelphia", "pittsburgh", "washington d c", "washington dc", "arlington va",
         "mclean", "reston", "bethesda", "minneapolis", "detroit", "phoenix", "portland",
         "salt lake city", "nashville", "raleigh", "durham", "charlotte", "st louis", "kansas city")),
    "canada": ("canada",
        ("canada", "canadian"),
        ("CA", "CAN"),
        ("ontario", "british columbia", "quebec", "alberta", "manitoba", "nova scotia",
         "saskatchewan", "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary",
         "edmonton", "waterloo", "kitchener", "mississauga", "victoria bc", "halifax", "winnipeg")),
    "united kingdom": ("uk",
        ("united kingdom", "uk", "u k", "great britain", "britain", "england", "scotland", "wales",
         "northern ireland"),
        ("GB", "UK"),
        ("london", "manchester", "birmingham", "leeds", "liverpool", "bristol", "cambridge uk",
         "oxford", "edinburgh", "glasgow", "belfast", "cardiff", "sheffield", "newcastle",
         "nottingham", "brighton", "reading", "milton keynes")),
    "australia": ("australia",
        ("australia", "australian"),
        ("AU", "AUS"),
        ("new south wales", "victoria australia", "queensland", "western australia",
         "south australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
         "gold coast", "hobart")),
    "united arab emirates": ("united arab emirates",
        ("united arab emirates", "uae", "emirates"),
        ("AE",),
        ("dubai", "abu dhabi", "sharjah", "difc")),
    "singapore": ("singapore", ("singapore",), ("SG",), ()),
    "new zealand": ("new zealand", ("new zealand",), ("NZ",), ("auckland", "wellington", "christchurch")),
    "ireland": ("ireland", ("ireland", "republic of ireland", "eire"), ("IE",), ("dublin", "cork", "galway", "limerick")),
    "germany": ("germany", ("germany", "deutschland", "german"), ("DE",),
        ("berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "köln", "stuttgart",
         "düsseldorf", "dusseldorf", "leipzig", "karlsruhe")),
    "netherlands": ("netherlands", ("netherlands", "the netherlands", "holland", "dutch"), ("NL",),
        ("amsterdam", "rotterdam", "the hague", "den haag", "utrecht", "eindhoven", "delft")),
    "france": ("france", ("france", "french"), ("FR",),
        ("paris", "lyon", "toulouse", "marseille", "bordeaux", "lille", "nantes", "grenoble", "sophia antipolis")),
    "india": ("india", ("india", "bharat"), ("IN",),
        ("bangalore", "bengaluru", "hyderabad", "mumbai", "pune", "delhi", "new delhi", "gurgaon",
         "gurugram", "noida", "chennai", "kolkata", "ahmedabad")),
    "bangladesh": ("bangladesh", ("bangladesh",), ("BD",), ("dhaka", "chattogram", "chittagong", "sylhet")),
    "japan": ("japan", ("japan", "japanese"), ("JP",), ("tokyo", "osaka", "kyoto", "yokohama", "fukuoka")),
    "hong kong": ("hong kong", ("hong kong", "hongkong"), ("HK",), ("kowloon",)),
    "south korea": ("south korea", ("south korea", "korea", "republic of korea"), ("KR",), ("seoul", "busan")),
    "china": ("china", ("china", "prc", "mainland china"), ("CN",), ("beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou")),
    "malaysia": ("malaysia", ("malaysia",), ("MY",), ("kuala lumpur", "penang", "johor")),
    "indonesia": ("indonesia", ("indonesia",), ("ID",), ("jakarta", "bandung", "surabaya")),
    "philippines": ("philippines", ("philippines", "the philippines"), ("PH",), ("manila", "makati", "cebu", "taguig")),
    "vietnam": ("vietnam", ("vietnam", "viet nam"), ("VN",), ("hanoi", "ho chi minh", "da nang")),
    "thailand": ("thailand", ("thailand",), ("TH",), ("bangkok", "chiang mai")),
    "pakistan": ("pakistan", ("pakistan",), ("PK",), ("karachi", "lahore", "islamabad")),
    "spain": ("spain", ("spain", "espana", "españa", "spanish"), ("ES",), ("madrid", "barcelona", "valencia", "seville", "malaga", "málaga")),
    "portugal": ("portugal", ("portugal",), ("PT",), ("lisbon", "lisboa", "porto", "braga")),
    "italy": ("italy", ("italy", "italia"), ("IT",), ("milan", "milano", "rome", "roma", "turin", "torino", "bologna")),
    "switzerland": ("switzerland", ("switzerland", "suisse", "schweiz"), ("CH",), ("zurich", "zürich", "geneva", "genève", "lausanne", "basel", "zug")),
    "austria": ("austria", ("austria", "osterreich", "österreich"), ("AT",), ("vienna", "wien", "graz", "linz")),
    "belgium": ("belgium", ("belgium", "belgique"), ("BE",), ("brussels", "bruxelles", "antwerp", "ghent", "leuven")),
    "denmark": ("denmark", ("denmark", "danmark"), ("DK",), ("copenhagen", "københavn", "aarhus")),
    "sweden": ("sweden", ("sweden", "sverige"), ("SE",), ("stockholm", "gothenburg", "göteborg", "malmo", "malmö", "lund")),
    "norway": ("norway", ("norway", "norge"), ("NO",), ("oslo", "bergen", "trondheim")),
    "finland": ("finland", ("finland", "suomi"), ("FI",), ("helsinki", "espoo", "tampere")),
    "poland": ("poland", ("poland", "polska"), ("PL",), ("warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk", "gdańsk", "poznan", "poznań")),
    "czechia": ("czech republic", ("czechia", "czech republic", "czech"), ("CZ",), ("prague", "praha", "brno")),
    "romania": ("romania", ("romania",), ("RO",), ("bucharest", "bucuresti", "cluj", "timisoara", "iasi")),
    "ukraine": ("ukraine", ("ukraine",), ("UA",), ("kyiv", "kiev", "lviv", "kharkiv", "odesa")),
    "turkey": ("turkey", ("turkey", "türkiye", "turkiye"), ("TR",), ("istanbul", "ankara", "izmir")),
    "israel": ("israel", ("israel",), ("IL",), ("tel aviv", "jerusalem", "haifa", "herzliya", "ramat gan")),
    "saudi arabia": ("saudi arabia", ("saudi arabia", "ksa", "saudi"), ("SA",), ("riyadh", "jeddah", "dammam", "neom")),
    "qatar": ("qatar", ("qatar",), ("QA",), ("doha",)),
    "egypt": ("egypt", ("egypt",), ("EG",), ("cairo", "alexandria", "giza")),
    "nigeria": ("nigeria", ("nigeria",), ("NG",), ("lagos", "abuja")),
    "kenya": ("kenya", ("kenya",), ("KE",), ("nairobi", "mombasa")),
    "south africa": ("south africa", ("south africa",), ("ZA",), ("cape town", "johannesburg", "durban", "pretoria")),
    "brazil": ("brazil", ("brazil", "brasil"), ("BR",), ("sao paulo", "são paulo", "rio de janeiro", "belo horizonte", "curitiba")),
    "mexico": ("mexico", ("mexico", "méxico"), ("MX",), ("mexico city", "ciudad de mexico", "cdmx", "guadalajara", "monterrey")),
    "argentina": ("argentina", ("argentina",), ("AR",), ("buenos aires", "cordoba", "rosario")),
    "chile": ("chile", ("chile",), ("CL",), ("santiago",)),
    "colombia": ("colombia", ("colombia",), ("CO",), ("bogota", "bogotá", "medellin", "medellín")),
}

# Region codes: a state/province abbreviation, not a country. Kept apart from the ISO codes above
# because "CA" is California in "San Francisco, CA" and Canada in "Montreal, QC, CA".
_REGION_CODES: dict[str, str] = {
    **{c: "united states" for c in (
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
        "IA", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE",
        "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
        "VA", "VT", "WA", "WI", "WV", "WY")},
    **{c: "canada" for c in ("ON", "BC", "QC", "AB", "MB", "NS", "NB", "SK", "PE", "YT", "NT", "NU")},
    **{c: "australia" for c in ("NSW", "VIC", "QLD", "ACT", "TAS", "NTL")},
}
# Newfoundland (NL) and Saskatchewan-style overlaps with an ISO code are left out on purpose: an
# unknown region code falls through to the ISO lookup, which is the better guess for a trailing field.

# ISO / colloquial country codes.
_ISO: dict[str, str] = {}
for _name, (_ci, _aliases, _codes, _places) in _C.items():
    for _c in _codes:
        _ISO.setdefault(_c, _name)

# alias -> canonical country, longest alias first so "united states" beats "us"
_ALIAS: dict[str, str] = {}
for _name, (_ci, _aliases, _codes, _places) in _C.items():
    for _a in (_name, *_aliases, *_places):
        _ALIAS.setdefault(_a, _name)
_ALIAS_ORDER = sorted(_ALIAS, key=len, reverse=True)

_SEP_RE = re.compile(r"[^a-z0-9]+")
_FIELD_RE = re.compile(r"[,;|/]|\s+-\s+")
_CODE_ONLY_RE = re.compile(r"^[A-Z]{2,3}[0-9]?$")

# Exact country markers only. City/region aliases deliberately stay out: a trailing "Australia" or
# "AU" must overrule the Canadian city named Waterloo, while "Toronto, Ontario" still needs the
# broader place-name inference below.
_COUNTRY_MARKERS: dict[str, str] = {}
for _name, (_ci, _aliases, _codes, _places) in _C.items():
    for _marker in (_name, *_aliases):
        _COUNTRY_MARKERS.setdefault(_SEP_RE.sub(" ", _marker.lower()).strip(), _name)


def _norm(s: str | None) -> str:
    """Lowercased, punctuation-free, space-padded so phrase lookups are word-safe."""
    return " " + _SEP_RE.sub(" ", (s or "").lower()).strip() + " "


def _code_countries(s: str | None) -> set[str]:
    """Countries implied by bare uppercase codes, read by position.

    "City, REGION, COUNTRY" is the shape boards use, so the trailing field of a three-part location is
    a country code and anything earlier is a region code — that is what tells "San Francisco, CA"
    (California) apart from "St. John's, NL, CA" (Canada).
    """
    fields = [f.strip() for f in _FIELD_RE.split(s or "") if f.strip()]
    codes = [(i, f.upper()) for i, f in enumerate(fields) if _CODE_ONLY_RE.match(f)]
    out: set[str] = set()
    for i, code in codes:
        trailing = i == len(fields) - 1 and len(fields) >= 3
        if not trailing and code in _REGION_CODES:
            out.add(_REGION_CODES[code])
        elif code in _ISO:
            out.add(_ISO[code])
        elif code in _REGION_CODES:
            out.add(_REGION_CODES[code])
    return out


def _has(norm: str, phrase: str) -> bool:
    """Whole-phrase lookup inside a _norm()'d string. `phrase` may be raw user input."""
    p = _SEP_RE.sub(" ", (phrase or "").lower()).strip()
    return bool(p) and f" {p} " in norm


def _infer_countries(text: str | None) -> set[str]:
    n = _norm(text)
    return {_ALIAS[a] for a in _ALIAS_ORDER if _has(n, a)}


def _code_country(code: str, preceding: list[str], index: int, field_count: int) -> str | None:
    """Resolve a bare code, using the preceding place to disambiguate CA/DE/IN-style overlaps."""
    candidates = {c for c in (_REGION_CODES.get(code), _ISO.get(code)) if c}
    if len(candidates) > 1 and preceding:
        contextual = _infer_countries(", ".join(preceding)) & candidates
        if len(contextual) == 1:
            return next(iter(contextual))
    trailing_country = index == field_count - 1 and field_count >= 3
    if not trailing_country and code in _REGION_CODES:
        return _REGION_CODES[code]
    return _ISO.get(code) or _REGION_CODES.get(code)


def is_remote_text(location: str | None) -> bool:
    n = _norm(location)
    return any(_has(n, w) for w in REMOTE_WORDS)


def countries_named(location: str | None) -> set[str]:
    """Canonical countries this location string points at ('Toronto, ON' -> {'canada'})."""
    fields = [f.strip() for f in _FIELD_RE.split(location or "") if f.strip()]
    found: set[str] = set()
    run: list[str] = []
    for i, field in enumerate(fields):
        marker = _COUNTRY_MARKERS.get(_SEP_RE.sub(" ", field.lower()).strip())
        if marker is None and _CODE_ONLY_RE.match(field):
            marker = _code_country(field.upper(), run, i, len(fields))
        if marker:
            found.add(marker)
            run.clear()  # the explicit marker owns the city/region fields immediately before it
        else:
            run.append(field)
    found |= _infer_countries(", ".join(run))
    return found or _infer_countries(location)


def canonical(term: str) -> str | None:
    """'UAE' -> 'united arab emirates'; None when the term is not a country we know."""
    n = _norm(term)
    for a in _ALIAS_ORDER:
        if _has(n, a):
            return _ALIAS[a]
    codes = _code_countries(term)
    return next(iter(codes)) if len(codes) == 1 else None


def country_for(location: str) -> str:
    """jobspy's `country_indeed` value for a location label."""
    name = canonical(location)
    return _C[name][0] if name else "worldwide"


def matches(job_location: str | None, job_remote: bool, wanted: list[str] | None) -> bool:
    """True when the posting sits in one of the wanted locations.

    A wanted "Remote" accepts a remote posting unless the posting pins itself to a country that was
    not asked for — "Remote - United States" is not a hit for someone who asked for Remote + Canada.
    """
    if not wanted:
        return True
    named = countries_named(job_location)
    wanted_countries = {c for c in (canonical(w) for w in wanted) if c}
    for term in wanted:
        t = (term or "").strip()
        if not t:
            continue
        if _norm(t).strip() in REMOTE_WORDS or is_remote_text(t):
            if job_remote or is_remote_text(job_location):
                if not named or named & wanted_countries:
                    return True
            continue
        name = canonical(t)
        if name:
            if name in named:
                return True
        elif _has(_norm(job_location), t):  # free-text term like "EMEA" or a city we don't list
            return True
    return False


def is_fresh(posted_at: str | None, found_at: str | None, max_age_days: int | None) -> bool:
    """Posted (or, failing that, first seen) within max_age_days. None = no date filter."""
    if not max_age_days:
        return True
    cutoff = date.today() - timedelta(days=max_age_days)
    for raw in (posted_at, found_at):
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw)[:10]).date() >= cutoff
        except ValueError:
            continue
    return False  # no usable date: cannot claim it is recent


def filter_jobs(jobs, wanted: list[str] | None, max_age_days: int | None) -> tuple[list, dict[str, int]]:
    """Split postings into (kept, {'location': n, 'stale': n}) — used before storing and scoring."""
    kept, dropped = [], {"location": 0, "stale": 0}
    for j in jobs:
        if not matches(getattr(j, "location", ""), bool(getattr(j, "remote", False)), wanted):
            dropped["location"] += 1
            continue
        if not is_fresh(getattr(j, "posted_at", None), getattr(j, "found_at", None), max_age_days):
            dropped["stale"] += 1
            continue
        kept.append(j)
    return kept, dropped
