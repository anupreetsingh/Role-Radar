"""Which countries a job's location text points to: US, CA (Canada), AU, IN (India), or OTHER.

Used by scripts/tag_countries.py. Cities come from GeoNames (see default_classifier).

Evidence, strongest first, within each part of a location ("A; B" and "A / B" are two parts):
  1. a country's name ("United States", "India", "USA", "U.S.", uppercase "US"/"AU"/"UK")
  2. a city next to a state/province code: the code is read as a US state, Canadian province,
     Australian or Indian state, or ISO country, whichever has a city of that name
     ("Columbus, IN" is Indiana; "Pune, IN" India; "Perth, WA" Western Australia)
  3. a state or province's full name ("Karnataka", "Ontario", "Texas")
  4. a code on its own, when it can only mean one country ("TX", "ON"); "CA" and "IN" alone can't
  5. a city on its own, by population, if no other country has a city nearly as large
     ("Bengaluru" India, "London" UK, but "Cambridge" and "Richmond" are left undecided)
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

TARGETS = ("US", "CA", "AU", "IN")

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia", "PR": "Puerto Rico",
}
CA_PROVINCES = {  # GeoNames admin1 number -> postal code, name
    "01": ("AB", "Alberta"), "02": ("BC", "British Columbia"), "03": ("MB", "Manitoba"),
    "04": ("NB", "New Brunswick"), "05": ("NL", "Newfoundland and Labrador"), "07": ("NS", "Nova Scotia"),
    "08": ("ON", "Ontario"), "09": ("PE", "Prince Edward Island"), "10": ("QC", "Quebec"),
    "11": ("SK", "Saskatchewan"), "12": ("YT", "Yukon"), "13": ("NT", "Northwest Territories"), "14": ("NU", "Nunavut"),
}
AU_STATES = {
    "01": ("ACT", "Australian Capital Territory"), "02": ("NSW", "New South Wales"), "03": ("NT", "Northern Territory"),
    "04": ("QLD", "Queensland"), "05": ("SA", "South Australia"), "06": ("TAS", "Tasmania"),
    "07": ("VIC", "Victoria"), "08": ("WA", "Western Australia"),
}
IN_STATES = {  # the codes Indian postings use (vehicle-plate style)
    "19": ("KA", "Karnataka"), "16": ("MH", "Maharashtra"), "40": ("TS", "Telangana"), "25": ("TN", "Tamil Nadu"),
    "07": ("DL", "Delhi"), "10": ("HR", "Haryana"), "36": ("UP", "Uttar Pradesh"), "13": ("KL", "Kerala"),
    "09": ("GJ", "Gujarat"), "28": ("WB", "West Bengal"), "02": ("AP", "Andhra Pradesh"), "24": ("RJ", "Rajasthan"),
    "21": ("OD", "Odisha"), "35": ("MP", "Madhya Pradesh"), "23": ("PB", "Punjab"), "33": ("GA", "Goa"),
    "05": ("CH", "Chandigarh"), "34": ("BR", "Bihar"), "39": ("UK", "Uttarakhand"), "03": ("AS", "Assam"),
    "38": ("JH", "Jharkhand"), "37": ("CG", "Chhattisgarh"), "22": ("PY", "Puducherry"), "11": ("HP", "Himachal Pradesh"),
    "12": ("JK", "Jammu and Kashmir"),
}
IN_EXTRA_STATE_CODES = {"TG": "40", "OR": "21", "NCR": "07"}

# Country names -> code. Target countries plus the others postings name most often, so their
# cities aren't mistaken for a target country's (e.g. "Tbilisi, Georgia").
COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "u.s.a": "US", "us": "US", "america": None, "canada": "CA", "australia": "AU", "india": "IN",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "great britain": "GB",
    "northern ireland": "GB", "ireland": "IE", "germany": "DE", "france": "FR", "spain": "ES", "portugal": "PT",
    "italy": "IT", "netherlands": "NL", "the netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "poland": "PL", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "greece": "GR", "turkey": "TR", "türkiye": "TR", "turkiye": "TR",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "iceland": "IS", "estonia": "EE",
    "latvia": "LV", "lithuania": "LT", "ukraine": "UA", "serbia": "RS", "croatia": "HR", "slovenia": "SI",
    "bosnia and herzegovina": "BA", "cyprus": "CY", "malta": "MT", "luxembourg": "LU", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "qatar": "QA", "egypt": "EG",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "morocco": "MA", "mauritius": "MU",
    "singapore": "SG", "japan": "JP", "china": "CN", "hong kong": "HK", "taiwan": "TW", "south korea": "KR",
    "korea": "KR", "republic of korea": "KR", "philippines": "PH", "vietnam": "VN", "viet nam": "VN",
    "thailand": "TH", "malaysia": "MY", "indonesia": "ID", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP", "new zealand": "NZ", "mexico": "MX", "méxico": "MX", "brazil": "BR",
    "brasil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY",
    "costa rica": "CR", "guatemala": "GT", "puerto rico": "US", "armenia": "AM", "georgia (country)": "GE",
    "kazakhstan": "KZ", "kyrgyzstan": "KG", "uzbekistan": "UZ", "jordan": "JO", "lebanon": "LB",
    "europe": "OTHER", "emea": "OTHER", "latam": "OTHER", "apac": "OTHER", "asia": "OTHER", "africa": "OTHER",
}
UPPER_ONLY = {"us", "uk"}  # names that count only in capitals ("us" is a word)
# Country names safe to find inside longer text ("Saudi Arabia Site Offices"); not "Mexico" (New
# Mexico), "Jordan" (West Jordan, UT), "Georgia", "Lebanon" or "Peru", which are also US places.
IN_TEXT = re.compile(r"\b(United States(?: of America)?|(?<!La )Canada|Australia|India|United Kingdom|Saudi Arabia|"
                     r"United Arab Emirates|South Africa|New Zealand|Costa Rica|Sri Lanka|Hong Kong|South Korea|"
                     r"Czech Republic|Germany|Singapore|Japan|Philippines|Poland|Brazil|Israel|Netherlands|France|"
                     r"Spain|Italy|Sweden|Switzerland|Austria|Belgium|Denmark|Norway|Finland|Portugal|Romania|"
                     r"Ukraine|Vietnam|Viet Nam|Thailand|Malaysia|Indonesia|Pakistan|Bangladesh|Nigeria|Kenya|Egypt|"
                     r"Argentina|Uruguay|Taiwan|Hungary|Greece|Estonia|Latvia|Lithuania|Serbia|Croatia|Bulgaria)\b", re.I)
ISO3 = {"USA": "US", "CAN": "CA", "AUS": "AU", "IND": "IN", "GBR": "GB", "DEU": "DE", "FRA": "FR", "ESP": "ES",
        "MEX": "MX", "BRA": "BR", "JPN": "JP", "CHN": "CN", "SGP": "SG", "ISR": "IL", "IRL": "IE", "NLD": "NL",
        "POL": "PL", "PHL": "PH", "KOR": "KR", "TWN": "TW", "SAU": "SA", "ARE": "AE", "CHE": "CH", "SWE": "SE",
        "ROU": "RO", "PRT": "PT", "ITA": "IT", "COL": "CO", "ARG": "AR", "NZL": "NZ", "ZAF": "ZA", "HKG": "HK"}

# Words that are never a city here (some are also GeoNames names).
NOT_CITIES = {
    "remote", "hybrid", "onsite", "on site", "on-site", "office", "home", "home office", "field", "virtual",
    "anywhere", "global", "worldwide", "headquarters", "hq", "multiple locations", "various", "nationwide",
    "national", "north", "south", "east", "west", "central", "metro", "area", "region", "united", "states",
    "flexible", "distributed", "travel", "tbd", "n/a", "other", "store", "corporate", "campus", "downtown",
    "north america", "americas", "latin america", "south america", "midwest", "northeast", "southeast",
    "southwest", "northwest", "west coast", "east coast", "bay area", "work from home", "wfh", "telecommute",
    "manufacturing", "plant", "warehouse", "distribution center", "hospital", "clinic", "main", "lab",
    "mission", "commerce", "industry", "enterprise", "republic", "liberty", "independence", "opportunity",
    "progress", "harmony", "unity", "friendship", "security", "success", "justice", "energy", "union",
    "university", "college", "garden", "university park", "research triangle park", "rtp",
}
ALIASES = {  # spellings postings use that GeoNames lists under another name
    "bangalore": "bengaluru", "bengalore": "bengaluru", "bombay": "mumbai", "madras": "chennai",
    "calcutta": "kolkata", "gurgaon": "gurugram", "trivandrum": "thiruvananthapuram", "cochin": "kochi",
    "poona": "pune", "baroda": "vadodara", "vizag": "visakhapatnam", "mysore": "mysuru",
    "mangalore": "mangaluru", "pondicherry": "puducherry", "allahabad": "prayagraj",
    "delhi ncr": "delhi", "ncr": "delhi", "greater noida": "noida",
    "nyc": "new york city", "new york": "new york city", "sf": "san francisco", "la": "los angeles",
    "dc": "washington", "washington dc": "washington", "washington d.c.": "washington",
    "montréal": "montreal", "quebec city": "quebec", "québec": "quebec", "ho chi minh": "ho chi minh city",
    "st. louis": "saint louis", "st louis": "saint louis", "st. paul": "saint paul", "st paul": "saint paul",
    "st. petersburg": "saint petersburg", "ft. lauderdale": "fort lauderdale", "ft lauderdale": "fort lauderdale",
    "ft. worth": "fort worth", "ft worth": "fort worth", "silicon valley": "san jose", "hyderabad ts": "hyderabad",
    "tel-aviv": "tel aviv", "bengaluru urban": "bengaluru",
}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", text).strip(" .,'\"")


class Gazetteer:
    """GeoNames cities (population 5,000+) by name: [(country, region code, population)]."""

    def __init__(self, cities: Path) -> None:
        self.by_name: dict[str, list[tuple[str, str | None, int]]] = defaultdict(list)
        self.countries: set[str] = set()
        with open(cities, encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                cc, admin, pop = f[8], f[10], int(f[14] or 0)
                region = _region_code(cc, admin)
                self.countries.add(cc)
                names = {fold(f[1]), fold(f[2])}
                if pop >= 100_000:  # big cities' other spellings (Bangalore, Montréal, Gurgaon)
                    names |= {fold(a) for a in f[3].split(",") if a.isascii() and len(a) >= 4}
                for name in names - NOT_CITIES:
                    if name:
                        self.by_name[name].append((cc, region, pop))

    def lookup(self, name: str) -> list[tuple[str, str | None, int]]:
        key = fold(name)
        key = ALIASES.get(key, key)
        if key in NOT_CITIES or len(key) < 3:
            return []
        return self.by_name.get(key, [])


def _region_code(cc: str, admin: str) -> str | None:
    if cc == "US":
        return admin
    table = {"CA": CA_PROVINCES, "AU": AU_STATES, "IN": IN_STATES}.get(cc)
    return table[admin][0] if table and admin in table else None


REGION_NAMES: dict[str, tuple[str, str]] = {}
for _code, _name in US_STATES.items():
    REGION_NAMES[fold(_name)] = ("US", _code)
for _table, _cc in ((CA_PROVINCES, "CA"), (AU_STATES, "AU"), (IN_STATES, "IN")):
    for _code, _name in _table.values():
        REGION_NAMES[fold(_name)] = (_cc, _code)
REGION_NAMES.update({"newfoundland": ("CA", "NL"), "quebec": ("CA", "QC"), "pei": ("CA", "PE"),
                     "orissa": ("IN", "OD"), "telengana": ("IN", "TS"), "tamilnadu": ("IN", "TN"),
                     "washington d.c.": ("US", "DC"), "washington dc": ("US", "DC")})
# Full names that are also cities or countries: evidence only when nothing better is there.
WEAK_REGIONS = {"victoria", "ontario", "georgia", "washington", "delhi", "punjab", "new brunswick", "goa",
                "chandigarh", "puducherry", "new york", "district of columbia"}

REGION_CODES: dict[str, list[tuple[str, str]]] = defaultdict(list)
for _code in US_STATES:
    REGION_CODES[_code].append(("US", _code))
for _table, _cc in ((CA_PROVINCES, "CA"), (AU_STATES, "AU"), (IN_STATES, "IN")):
    for _code, _name in _table.values():
        REGION_CODES[_code].append((_cc, _code))
for _code, _admin in IN_EXTRA_STATE_CODES.items():
    REGION_CODES[_code].append(("IN", IN_STATES[_admin][0]))
# Codes alone (no city beside them) that can only mean one target country.
LONE_CODES = {code: "US" for code in US_STATES if code not in {"CA", "IN", "WA", "DE", "IL", "CO", "AR", "PA",
                                                               "MA", "ME", "OR", "OK", "HI", "ID", "GA", "AL",
                                                               "MT", "MD", "SC", "NE", "SD", "LA", "MS", "MN"}}
LONE_CODES.update({"WA": "US", "ON": "CA", "BC": "CA", "QC": "CA", "AB": "CA", "MB": "CA", "NS": "CA",
                   "NB": "CA", "NSW": "AU", "VIC": "AU", "QLD": "AU", "TAS": "AU",
                   "ACT": "AU", "KA": "IN", "MH": "IN", "TS": "IN", "TN": "US", "DL": "IN"})
LONE_CODES = {k: v for k, v in LONE_CODES.items() if v}

SPLIT_PARTS = re.compile(r";|\||\n|\s/\s|(?<=[a-z\)])/(?=\s?[A-Z])|\bor\b|\s&\s|\band\b(?=\s+[A-Z])")
SPLIT_PIECES = re.compile(r"[,:()\[\]{}]|\s[-–—~]\s|\s[-–—]|[-–—]\s|\.\s|\s{2,}|\s?•\s?")
CODE = re.compile(r"^[A-Z]{2,3}$")
ZIP_US = re.compile(r"\b\d{5}(?:-\d{4})?\b")
POSTAL_CA = re.compile(r"\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b")


class Classifier:
    def __init__(self, gazetteer: Gazetteer) -> None:
        self.gaz = gazetteer

    @lru_cache(maxsize=None)
    def countries(self, location: str | None) -> frozenset[str]:
        """The countries a location names: any of US, CA, AU, IN, and OTHER for the rest."""
        if not location or re.fullmatch(r"\s*\d+\s+locations?\s*", location, re.I):
            return frozenset()
        found: set[str] = set()
        for part in SPLIT_PARTS.split(location):
            if part and part.strip():
                found |= self._part(part)
        return frozenset(found)

    def _part(self, part: str) -> set[str]:
        text = re.sub(r"\b([A-Z]{2,3})[-._]", r"\1, ", part)      # US-CA-Menlo Park, IN-KA-Bangalore, USA.VA.Reston
        text = re.sub(r"_", " ", text)
        text = re.sub(r"-([A-Z]{2,3})\b", r", \1", text)          # Field-NY
        pieces = [p.strip(" .-–—*'\"#") for p in SPLIT_PIECES.split(text)]
        pieces = [p for p in pieces if p and not re.fullmatch(r"[\d\s-]+", p)]
        expanded: list[str] = []
        for p in pieces:  # "Tucker GA", "USA WA Spokane", "CA 92626": split off codes at either end
            words = p.split()
            while len(words) > 1 and (CODE.fullmatch(words[0]) or fold(words[0]) in COUNTRY_NAMES and words[0].isupper()):
                expanded.append(words.pop(0))
            tail = []
            while len(words) > 1 and (CODE.fullmatch(words[-1]) or re.fullmatch(r"\d{5}(-\d{4})?", words[-1])):
                tail.insert(0, words.pop())
            expanded.append(" ".join(words))
            expanded.extend(t for t in tail if not t[0].isdigit())
        pieces = [p for p in expanded if p]

        strong: set[str] = set()
        for p in pieces:
            country = self._country(p)
            if country:
                strong.add(country)
        if strong:
            return {c if c in TARGETS else "OTHER" for c in strong}
        if ZIP_US.search(part) and any(CODE.fullmatch(p) and p in US_STATES for p in pieces):
            return {"US"}
        if POSTAL_CA.search(part):
            return {"CA"}

        codes = [i for i, p in enumerate(pieces) if CODE.fullmatch(p) and (p in REGION_CODES or p in self.gaz.countries)]
        for i in codes:  # a known city next to a code: the piece before, then after
            for j in (i - 1, i + 1):
                if 0 <= j < len(pieces) and j not in codes:
                    got = self._pair(pieces[j], pieces[i])
                    if got:
                        return {got}
        for k, p in enumerate(list(pieces)):  # "Burlington Massachusetts"
            words = p.split()
            for n in (3, 2, 1):
                tail = fold(" ".join(words[-n:]))
                if len(words) > n and tail in REGION_NAMES and tail not in WEAK_REGIONS:
                    pieces[k:k + 1] = [" ".join(words[:-n]), " ".join(words[-n:])]
                    break
        named = [p for p in pieces if fold(p) in REGION_NAMES]
        strong_regions = [REGION_NAMES[fold(p)] for p in named if fold(p) not in WEAK_REGIONS]
        if strong_regions:
            return {strong_regions[-1][0]}
        for i in codes:  # an unknown town beside a US state code is in the US
            if pieces[i] in US_STATES and any(0 <= j < len(pieces) and j not in codes for j in (i - 1, i + 1)):
                return {"US"}
        for i in codes:
            if pieces[i] in LONE_CODES:
                return {LONE_CODES[pieces[i]]}
        for p in pieces:
            got = self._city(p)
            if got:
                return {got}
        if named:
            return {REGION_NAMES[fold(named[-1])][0]}
        return set()

    def _country(self, piece: str) -> str | None:
        if piece in ISO3:
            return ISO3[piece]
        key = fold(piece)
        if key in COUNTRY_NAMES:
            if key in UPPER_ONLY and not piece.replace(".", "").isupper():
                return None
            return COUNTRY_NAMES[key]
        # "Remote US", "US Remote", "Remote - Canada (Hybrid)"
        for word in re.findall(r"[A-Za-z.]+", piece):
            if word in ("US", "USA", "U.S.", "U.S", "U.S.A.", "U.S.A", "UK", "AU"):
                return {"UK": "GB", "AU": "AU"}.get(word, "US")
        m = IN_TEXT.search(piece)
        if m:
            return COUNTRY_NAMES[fold(m.group(1))]
        return None

    def _pair(self, city: str, code: str) -> str | None:
        """The country of "city, code", if the city is in that state/province or country."""
        options = self._cities(city)
        best: tuple[int, str] | None = None
        for cc, region, pop in options:  # a state or province of that name first
            if (cc, region) in {(c, r) for c, r in REGION_CODES.get(code, [])}:
                if best is None or pop > best[0]:
                    best = (pop, cc)
        if best:
            return best[1] if best[1] in TARGETS else "OTHER"
        if code in self.gaz.countries:  # then an ISO country ("Toronto, CA", "Pune, IN", "Berlin, DE")
            if any(cc == code for cc, _, _ in options):
                return code if code in TARGETS else "OTHER"
        return None

    def _cities(self, piece: str) -> list[tuple[str, str | None, int]]:
        piece = _bare(piece)
        found = self.gaz.lookup(piece)
        if found:
            return found
        words = re.sub(r"^\d+\s+", "", piece).split()  # "280 Bristol St Costa Mesa", "Bangalore Kar"
        for n in (3, 2, 1):
            for chunk in (words[-n:], words[:n]):
                if len(words) > n:
                    found = self.gaz.lookup(" ".join(chunk))
                    if found:
                        return found
        return []

    def _city(self, piece: str) -> str | None:
        """A city alone: the country with the largest city of that name, if clearly the largest."""
        options = self.gaz.lookup(_bare(piece))
        if not options:
            return None
        by_country: dict[str, int] = {}
        for cc, _, pop in options:
            by_country[cc] = max(by_country.get(cc, 0), pop)
        ranked = sorted(by_country.items(), key=lambda kv: -kv[1])
        top_cc, top = ranked[0]
        if top < 50_000:
            return None
        if len(ranked) > 1 and ranked[1][1] * 1.5 > top:
            return None
        return top_cc if top_cc in TARGETS else "OTHER"


AROUND = re.compile(r"^(?:greater|metro|downtown|central|hybrid|remote|onsite|on-site|in-office)\s+|"
                    r"\s+(?:(?:bay|metro(?:politain|politan)?|greater)\s+)?(?:area|office|campus|hq|headquarters|"
                    r"metro|region|hub|remote|hybrid|onsite|on-site)(?:\s+\w+)?$", re.I)


def _bare(piece: str) -> str:
    """"Greater Seattle Area" -> "Seattle"; "New York Office" -> "New York"."""
    for _ in range(2):
        piece = AROUND.sub("", piece).strip()
    return piece


GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
CACHE = Path.home() / ".cache" / "role-radar"


@lru_cache(maxsize=1)
def default_classifier(cache: Path = CACHE) -> Classifier:
    """A classifier over GeoNames' cities of 5,000+ people (CC BY 4.0), downloaded once into `cache`."""
    cities = cache / "cities5000.txt"
    if not cities.exists():
        import io
        import urllib.request
        import zipfile

        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(GEONAMES_URL, timeout=120) as resp:
            zipfile.ZipFile(io.BytesIO(resp.read())).extract("cities5000.txt", cache)
    return Classifier(Gazetteer(cities))
