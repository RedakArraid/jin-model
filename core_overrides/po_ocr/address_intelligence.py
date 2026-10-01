from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import time
from functools import lru_cache
from dataclasses import dataclass
from typing import Any

from .models import Address
from .normalize import ascii_fold, clean_text



_BAN_CIRCUIT_OPEN_UNTIL = 0.0

STREET_TYPES = {
    "RUE": ["RUE", "R"],
    "AVENUE": ["AVENUE", "AV", "AV."],
    "BOULEVARD": ["BOULEVARD", "BD", "BD."],
    "ROUTE": ["ROUTE", "RTE", "RTE."],
    "CHEMIN": ["CHEMIN", "CHE"],
    "IMPASSE": ["IMPASSE", "IMP"],
    "ALLEE": ["ALLEE", "ALLÉE", "ALL", "ALL."],
    "PLACE": ["PLACE", "PL", "PL."],
    "QUAI": ["QUAI"],
    "PASSAGE": ["PASSAGE"],
    "SQUARE": ["SQUARE"],
    "VOIE": ["VOIE"],
    "ROND-POINT": ["ROND-POINT", "ROND POINT"],
    "LOTISSEMENT": ["LOTISSEMENT", "LOT"],
}

COUNTRIES = {
    "france": ("France", "FR"),
    "allemagne": ("Allemagne", "DE"), "germany": ("Germany", "DE"),
    "belgique": ("Belgique", "BE"), "belgium": ("Belgium", "BE"),
    "espagne": ("Espagne", "ES"), "spain": ("Spain", "ES"),
    "italie": ("Italie", "IT"), "italy": ("Italy", "IT"),
    "suisse": ("Suisse", "CH"), "switzerland": ("Switzerland", "CH"),
    "luxembourg": ("Luxembourg", "LU"),
    "pays bas": ("Pays-Bas", "NL"), "netherlands": ("Netherlands", "NL"),
    "royaume uni": ("Royaume-Uni", "GB"), "united kingdom": ("United Kingdom", "GB"),
}

META_TOKEN_RE = re.compile(
    r"\b(?:TEL|T[ÉE]L|TELEPHONE|PHONE|FAX|EMAIL|E-MAIL|MAIL|SIRET|SIREN|TVA|VAT|RCS|APE|IBAN|BIC)\b",
    re.I,
)

ROUTING_PATTERNS = {
    "po_box": re.compile(r"\bB\.?\s*P\.?\s*[:\-]?\s*(\d{2,10})\b", re.I),
    "tsa": re.compile(r"\bT\.?\s*S\.?\s*A\.?\s*[:\-]?\s*(\d{2,10})\b", re.I),
    "cs": re.compile(r"\bC\.?\s*S\.?\s*[:\-]?\s*(\d{2,10})\b", re.I),
}

BUILDING_RE = re.compile(r"\b(?:BATIMENT|B[ÂA]T\.?|IMMEUBLE|TOUR|BLOC|HALL)\b[^,;|]*", re.I)
RESIDENCE_RE = re.compile(r"\b(?:RESIDENCE|R[ÉE]SIDENCE)\b[^,;|]*", re.I)
ZONE_RE = re.compile(r"\b(?:Z\.?\s*I\.?|Z\.?\s*A\.?\s*C\.?|Z\.?\s*A\.?\s*E\.?|ZONE INDUSTRIELLE|ZONE D['’]?ACTIVIT[ÉE]S?)\b[^,;|]*", re.I)
PARK_RE = re.compile(r"\b(?:PARC D['’]?ACTIVIT[ÉE]S?|PARC ACTIVIT[ÉE]S?|BUSINESS PARK|TECHNOP[ÔO]LE)\b[^,;|]*", re.I)
LIEU_DIT_RE = re.compile(r"\b(?:LIEU[- ]DIT|LD)\b\s*[:\-]?\s*(.+)", re.I)
FLOOR_RE = re.compile(r"\b(?:ETAGE|[ÉE]TAGE|NIVEAU)\s*[:\-]?\s*([A-Z0-9\-]+)", re.I)
ENTRANCE_RE = re.compile(r"\b(?:ENTREE|ENTR[ÉE]E|PORTE)\s*[:\-]?\s*([A-Z0-9\-]+)", re.I)
UNIT_RE = re.compile(r"\b(?:APPARTEMENT|APPART|APT|UNIT[ÉE]?)\s*[:\-]?\s*([A-Z0-9\-]+)", re.I)


def _fold(value: str | None) -> str:
    return " ".join(ascii_fold(value or "").casefold().split())


def _uniq(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        c = clean_text(line)
        if not c:
            continue
        k = _fold(c)
        if k and k not in seen:
            out.append(c)
            seen.add(k)
    return out


def _source_lines(addr: Address) -> list[str]:
    values = list(addr.raw_lines or []) + [addr.line1, addr.line2, addr.line3]
    return _uniq([x for x in values if x])


_NON_POSTAL_LINE_RE = re.compile(
    r"^(?:CONTACT|CORRESPONDANT|A\s+L[' ]ATTENTION|ATTENTION|INFOS?|CONSIGNE|"
    r"EN\s+EXPRESS|MERCI\s+DE|CODE\s+D.?OUVERTURE|CODE\s+PORTE|DIGICODE|"
    r"HORAIRES?|RECEPTION|LIVRAISON\s+(?:AVANT|APRES|SUR)|ADHERENT)\b",
    re.I,
)
_NON_POSTAL_TAIL_RE = re.compile(
    r"(?:\s*[|;]\s*|\s{2,})(?:EN\s+EXPRESS|MERCI\s+DE|CODE\s+D.?OUVERTURE|"
    r"CODE\s+PORTE|DIGICODE|HORAIRES?|RECEPTION|CONTACT|CORRESPONDANT)\b.*$",
    re.I,
)


def _postal_source_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    drop_following_access_code = False
    for raw in lines:
        value = clean_text(raw).strip(" |;,-")
        folded = ascii_fold(value)
        if drop_following_access_code and re.fullmatch(r"[A-Z]?\d{4,10}", folded, flags=re.I):
            drop_following_access_code = False
            continue
        drop_following_access_code = False
        if not value or re.fullmatch(r"[*#_\-=. ]+", value) or re.match(r"^[*#_\-=. ]+ADHERENT\b", folded, flags=re.I):
            continue
        if _NON_POSTAL_LINE_RE.match(folded):
            drop_following_access_code = bool(re.match(r"^CODE\s+D.?OUVERTURE|^CODE\s+PORTE|^DIGICODE", folded, flags=re.I))
            continue
        value = _NON_POSTAL_TAIL_RE.sub("", value).strip(" |;,-")
        if not value or re.fullmatch(r"B\.?\s*P\.?", ascii_fold(value), flags=re.I):
            continue
        cleaned.append(value)
    return _uniq(cleaned)


def _strip_metadata_tail(value: str) -> str:
    parts = META_TOKEN_RE.split(value, maxsplit=1)
    return clean_text(parts[0]).strip(" ,;-")


def _street_regex() -> re.Pattern:
    aliases: list[str] = []
    for vals in STREET_TYPES.values():
        aliases.extend(vals)
    aliases = sorted(set(aliases), key=len, reverse=True)
    exp = "|".join(re.escape(x) for x in aliases)
    return re.compile(
        rf"^\s*(?:(?P<num>\d{{1,5}}(?:\s*(?:BIS|TER|QUATER|[A-Z]|[-/&]\s*\d{{1,5}}[A-Z]?))?)(?:\s*[,;]\s*|\s+))?(?P<type>{exp})\s+(?P<name>.+?)\s*$",
        re.I,
    )


STREET_RE = _street_regex()
POSTAL_ROLE_PREFIX_RE = re.compile(
    r"^(?:DESTINATAIRE|ADRESSE\s+(?:DE\s+)?(?:LIVRAISON|FACTURATION)|SHIP\s+TO|BILL\s+TO)\s*[:\-]?\s+",
    re.I,
)


def _canonical_street_type(raw: str | None) -> str | None:
    if not raw:
        return None
    f = _fold(raw).replace(".", "")
    for canon, aliases in STREET_TYPES.items():
        if any(f == _fold(a).replace(".", "") for a in aliases):
            return canon
    return clean_text(raw).upper()


def _split_city_cedex(text: str) -> tuple[str | None, bool | None, str | None]:
    value = clean_text(text).strip(" ,;-")
    if not value:
        return None, None, None
    m = re.search(r"\bCEDEX(?:\s+(\d{1,2}))?\b", value, flags=re.I)
    if not m:
        return value, None, None
    city = clean_text((value[:m.start()] + " " + value[m.end():]).strip(" ,;-"))
    return city or None, True, m.group(1)


def _extract_postal_city(lines: list[str], routing_spans: list[str]) -> tuple[str | None, str | None, bool | None, str | None]:
    routing_numbers = set(re.findall(r"\d{2,10}", " ".join(routing_spans)))
    # Prefer a line whose 5-digit code is followed by city text. This avoids BP/TSA/CS numbers.
    for line in reversed(lines):
        cleaned = _strip_metadata_tail(line)
        for m in re.finditer(r"(?<!\d)(\d{5})(?!\d)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .\-'/]{1,80})", cleaned):
            cp = m.group(1)
            if cp in routing_numbers:
                continue
            city, cedex, cedex_number = _split_city_cedex(m.group(2))
            if city:
                return cp, city, cedex, cedex_number
    # Conservative international fallback: 4-6 digits but never a routing number.
    for line in reversed(lines):
        cleaned = _strip_metadata_tail(line)
        for m in re.finditer(r"(?<!\d)(\d{4,6})(?!\d)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .\-'/]{1,80})", cleaned):
            if m.group(1) in routing_numbers:
                continue
            city, cedex, cedex_number = _split_city_cedex(m.group(2))
            if city:
                return m.group(1), city, cedex, cedex_number
    return None, None, None, None


def parse_address(addr: Address, *, infer_country: bool = True) -> tuple[Address, dict[str, float], list[str]]:
    """Parse postal components without inventing an address.

    Returns (normalized_address, per_component_confidence, warnings).
    Values inferred from a postcode are explicitly marked with lower confidence.
    """
    out = addr.model_copy(deep=True)
    source_lines = _source_lines(out)
    out.raw_lines = source_lines
    out.raw = out.raw or " | ".join(source_lines) if source_lines else out.raw
    lines = _postal_source_lines(source_lines)
    if source_lines:
        out.address_complement = None
    warnings: list[str] = []
    conf: dict[str, float] = {}

    joined = " | ".join(lines)
    folded = _fold(joined)

    # Routing complements first so their numbers cannot be mistaken for postcodes.
    routing_spans: list[str] = []
    for field, pat in ROUTING_PATTERNS.items():
        m = pat.search(joined)
        if m:
            prefix = {"po_box": "BP", "tsa": "TSA", "cs": "CS"}[field]
            setattr(out, field, f"{prefix} {m.group(1)}")
            conf[field] = 0.995
            routing_spans.append(m.group(0))

    # Building/residence/zone/complements.
    for field, pat in (
        ("building", BUILDING_RE), ("residence", RESIDENCE_RE),
        ("industrial_zone", ZONE_RE), ("business_park", PARK_RE),
    ):
        m = pat.search(joined)
        if m:
            setattr(out, field, clean_text(m.group(0)).strip(" ,;|"))
            conf[field] = 0.94
    m = LIEU_DIT_RE.search(joined)
    if m:
        out.lieu_dit = clean_text(m.group(1)).strip(" ,;|")
        conf["lieu_dit"] = 0.93
    for field, pat in (("floor", FLOOR_RE), ("entrance", ENTRANCE_RE), ("unit", UNIT_RE)):
        m = pat.search(joined)
        if m:
            setattr(out, field, clean_text(m.group(1)))
            conf[field] = 0.92

    # Postal code/city/CEDEX, avoiding routing numbers.
    cp, city, cedex, cedex_number = _extract_postal_city(lines, routing_spans)
    if cp:
        out.postal_code = cp
        conf["postal_code"] = 0.995
    if city:
        out.city = city
        conf["city"] = 0.985
    if cedex:
        out.cedex = True
        out.cedex_number = cedex_number
        conf["cedex"] = 0.995
        if cedex_number:
            conf["cedex_number"] = 0.99
    elif out.city:
        # Normalize CEDEX even when city/postcode were populated by an upstream parser.
        norm_city, norm_cedex, norm_cedex_number = _split_city_cedex(out.city)
        if norm_cedex:
            out.city = norm_city
            out.cedex = True
            out.cedex_number = norm_cedex_number
            conf.setdefault("city", 0.96)
            conf["cedex"] = 0.98
            if norm_cedex_number:
                conf["cedex_number"] = 0.97

    # If OCR dropped BP/TSA/CS but left two routing-like 5-digit codes on one line,
    # preserve the extra code generically instead of mislabelling it as the postcode.
    if out.postal_code and not (out.po_box or out.tsa or out.cs):
        candidates = []
        for line in lines:
            candidates.extend(re.findall(r"(?<!\d)(\d{5})(?!\d)", line))
        extras = [x for x in candidates if x != out.postal_code]
        if extras:
            out.postal_routing_code = extras[0]
            conf["postal_routing_code"] = 0.72
            warnings.append("untyped_postal_routing_code_inferred")

    # Street: prefer line-level parsing, excluding postal/routing/complement lines.
    best_street = None
    street_source_line = None
    for line in lines:
        low = _fold(line)
        if any(p.search(line) for p in ROUTING_PATTERNS.values()):
            # Inline BP can coexist with the street; remove only the routing portion.
            candidate = line
            for p in ROUTING_PATTERNS.values():
                candidate = p.sub("", candidate)
        else:
            candidate = line
        candidate = re.sub(r"\b\d{5}\s+[A-Za-zÀ-ÿ].*$", "", candidate).strip(" ,;-")
        if not candidate or META_TOKEN_RE.search(candidate):
            candidate = _strip_metadata_tail(candidate)
        # A neighbouring role label can share the street's extracted PDF row.
        # Remove it only when the remaining text is a numbered street; the
        # original line and the business role assigned upstream stay unchanged.
        prefix = POSTAL_ROLE_PREFIX_RE.match(candidate)
        if prefix:
            street_value = candidate[prefix.end():]
            numbered_street = STREET_RE.match(street_value)
            if numbered_street and numbered_street.group("num"):
                candidate = street_value
        m = STREET_RE.match(candidate)
        if not m:
            # Some sites use an ordinal before the street type, e.g. "1ERE AVENUE".
            suffix_aliases = sorted({a for vals in STREET_TYPES.values() for a in vals}, key=len, reverse=True)
            suffix_re = re.compile(r"^\s*(?P<name>.+?)\s+(?P<type>" + "|".join(re.escape(x) for x in suffix_aliases) + r")\s*$", re.I)
            sm2 = suffix_re.match(candidate)
            if sm2 and re.search(r"\d|ERE|ER|EME|ÈME", sm2.group("name"), flags=re.I):
                class _SuffixStreetMatch:
                    def group(self, name):
                        if name == "num": return None
                        if name == "type": return sm2.group("type")
                        if name == "name": return sm2.group("name")
                m = _SuffixStreetMatch()
        if not m:
            # ERP documents often print compound ranges as "124 126 RUE ...".
            m2 = re.match(r"^\s*(\d{1,5}[A-Za-z]?)\s+(\d{1,5}[A-Za-z]?)\s+(.+)$", candidate)
            if m2:
                tail = m2.group(3)
                sm = STREET_RE.match(tail)
                if sm:
                    class _StreetMatch:
                        def group(self, name):
                            if name == "num": return f"{m2.group(1)}-{m2.group(2)}"
                            return sm.group(name)
                    m = _StreetMatch()
        if m:
            best_street = m
            street_source_line = line
            break
    if best_street:
        num = clean_text(best_street.group("num")) if best_street.group("num") else None
        stype = _canonical_street_type(best_street.group("type"))
        name = clean_text(best_street.group("name")).strip(" ,;-")
        # Remove routing/zone/complement tokens accidentally carried after the street name.
        name = re.split(r"\b(?:BP|B\.?\s*P\.?|TSA|T\.?\s*S\.?\s*A\.?|CS|C\.?\s*S\.?|Z\.?\s*I\.?|Z\.?\s*A\.?\s*C\.?|Z\.?\s*A\.?\s*E\.?)\b", name, maxsplit=1, flags=re.I)[0].strip(" ,;-")
        out.house_number = num or out.house_number
        out.street_type = stype
        out.street_name = name or out.street_name
        out.street = " ".join(x for x in (stype, name) if x)
        if num:
            conf["house_number"] = 0.99
        conf["street_type"] = 0.985
        conf["street_name"] = 0.985
        conf["street"] = 0.985

    # Explicit country.
    explicit = None
    for token, value in COUNTRIES.items():
        if token in folded:
            explicit = value
            break
    if explicit:
        out.country, out.country_code = explicit
        conf["country"] = conf["country_code"] = 0.995
    elif infer_country and out.postal_code and re.fullmatch(r"\d{5}", out.postal_code):
        # French 5-digit postcodes are a strong hint, but this is an inference, not source truth.
        out.country = out.country or "France"
        out.country_code = out.country_code or "FR"
        conf.setdefault("country", 0.93)
        conf.setdefault("country_code", 0.93)
        warnings.append("country_inferred_from_postal_pattern")

    # Complement is anything useful not already typed.
    typed_folded = {_fold(x) for x in (
        out.building, out.residence, out.industrial_zone, out.business_park,
        out.po_box, out.tsa, out.cs, out.street, f"{out.postal_code or ''} {out.city or ''}".strip(),
        out.country, street_source_line,
    ) if x}
    complements: list[str] = []
    for line in lines:
        lf = _fold(line)
        if not lf or any(t and (lf == t or t in lf) for t in typed_folded):
            continue
        if META_TOKEN_RE.search(line):
            continue
        complements.append(line)
    if complements:
        out.address_complement = " | ".join(_uniq(complements))
        conf["address_complement"] = 0.78

    if out.postal_code and not out.city:
        warnings.append("city_missing")
    if out.city and not out.postal_code:
        warnings.append("postal_code_missing")
    if not out.street and not (out.lieu_dit or out.industrial_zone or out.business_park):
        warnings.append("street_or_locality_missing")

    return out, conf, warnings


def local_validation(addr: Address, component_conf: dict[str, float]) -> tuple[str, float, list[str]]:
    warnings: list[str] = []
    score = 0.0
    weights = {
        "street": 0.26, "postal_code": 0.22, "city": 0.22,
        "country_code": 0.08, "house_number": 0.08,
        "po_box": 0.04, "industrial_zone": 0.04, "building": 0.03, "address_complement": 0.03,
    }
    for key, weight in weights.items():
        if getattr(addr, key, None):
            score += weight * component_conf.get(key, component_conf.get("street_name", 0.85))
    if addr.postal_code and addr.country_code == "FR" and not re.fullmatch(r"\d{5}", addr.postal_code):
        warnings.append("invalid_french_postal_code_shape")
        score -= 0.25
    if addr.postal_code and addr.city and addr.postal_code in set(re.findall(r"\d+", " ".join(x for x in (addr.po_box, addr.tsa, addr.cs) if x))):
        warnings.append("postal_code_conflicts_with_routing_number")
        score -= 0.35
    score = max(0.0, min(1.0, score / 0.86))
    if score >= 0.83 and addr.postal_code and addr.city:
        status = "STRUCTURALLY_VALID"
    elif score >= 0.58:
        status = "PARTIAL"
    else:
        status = "INVALID_OR_INCOMPLETE"
    return status, round(score, 4), warnings


@dataclass
class VerificationResult:
    status: str
    provider: str | None = None
    score: float | None = None
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    result_type: str | None = None
    city: str | None = None
    postal_code: str | None = None
    insee_code: str | None = None
    context: str | None = None
    error: str | None = None


def verify_french_address(addr: Address, *, endpoint: str = "https://data.geopf.fr/geocodage/search", timeout: float = 3.5) -> VerificationResult:
    """Verify a French address against the official Géoplateforme/BAN-backed geocoder.

    Network failures never erase extracted data; they return UNAVAILABLE.
    """
    global _BAN_CIRCUIT_OPEN_UNTIL
    if time.time() < _BAN_CIRCUIT_OPEN_UNTIL:
        return VerificationResult(status="UNAVAILABLE", provider="IGN_GEOPLATEFORME_BAN", error="circuit_breaker_open")
    query_parts = [
        " ".join(x for x in (addr.house_number, addr.street) if x),
        addr.address_complement,
        " ".join(x for x in (addr.postal_code, addr.city) if x),
    ]
    q = " ".join(clean_text(x) for x in query_parts if clean_text(x))
    if not q:
        return VerificationResult(status="NOT_ENOUGH_DATA")
    url = endpoint + "?" + urllib.parse.urlencode({"q": q, "limit": 3, "returntruegeometry": "false"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JIN-Model-Address-Intelligence/5.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network environment dependent
        _BAN_CIRCUIT_OPEN_UNTIL = time.time() + 60.0
        return VerificationResult(status="UNAVAILABLE", provider="IGN_GEOPLATEFORME_BAN", error=str(exc)[:200])
    features = payload.get("features") or []
    if not features:
        return VerificationResult(status="NOT_FOUND", provider="IGN_GEOPLATEFORME_BAN", score=0.0)
    best = max(features, key=lambda f: float((f.get("properties") or {}).get("score") or 0.0))
    props: dict[str, Any] = best.get("properties") or {}
    geom = best.get("geometry") or {}
    coords = geom.get("coordinates") or [None, None]
    score = float(props.get("score") or 0.0)
    # BAN/geocoder score is the authority signal, but require city/postcode compatibility when supplied.
    mismatch = False
    if addr.postal_code and props.get("postcode") and str(addr.postal_code) != str(props.get("postcode")):
        mismatch = True
    if addr.city and props.get("city") and _fold(addr.city) != _fold(str(props.get("city"))):
        # Allow CEDEX/accents/qualifiers by substring compatibility.
        a, b = _fold(addr.city), _fold(str(props.get("city")))
        mismatch = not (a in b or b in a)
    status = "VERIFIED" if score >= 0.70 and not mismatch else ("AMBIGUOUS" if score >= 0.45 else "NOT_FOUND")
    return VerificationResult(
        status=status, provider="IGN_GEOPLATEFORME_BAN", score=round(score, 4),
        label=props.get("label") or props.get("name"),
        latitude=coords[1] if len(coords) > 1 else None, longitude=coords[0] if coords else None,
        result_type=props.get("type"), city=props.get("city"), postal_code=props.get("postcode"),
        insee_code=props.get("citycode"), context=props.get("context"),
    )
