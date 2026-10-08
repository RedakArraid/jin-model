from __future__ import annotations

import re
import statistics
from typing import Any

from po_ocr.models import PageResult, WordToken
from po_ocr.normalize import ascii_fold

from .models import ContentBlock, Entity, KeyValue, SourceRef, UniversalTable


DATE_RE = re.compile(r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
URL_RE = re.compile(r"\bhttps?://[^\s<>()]+", re.I)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
AMOUNT_RE = re.compile(r"(?<![\w.,])(?:EUR|USD|GBP|CHF|€|\$|£)?\s*[-+]?(?:(?:\d{1,3}(?:[ .'’]\d{3})+)|\d+)[.,]\d{2}\s*(?:EUR|USD|GBP|CHF|€|\$|£)?", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(?\d{1,4}\)?[ .-]?){2,5}\d{2,4}(?!\w)")
PERCENT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*%")

DOCUMENT_SIGNATURES: list[tuple[str, tuple[str, ...], int]] = [
    ("order_cancellation", ("demande d'annulation d'une commande", "annulation de commande", "order cancellation"), 1),
    ("remittance_advice", ("remittance advice", "avis de virement", "total virement", "date de valeur"), 2),
    ("approval_form", ("rb general approval form", "general approval form", "approver(s)", "approval workflow"), 1),
    ("invoice", ("invoice", "facture", "rechnung", "invoice number", "total ttc", "amount due"), 2),
    ("delivery_note", ("delivery note", "bon de livraison", "lieferschein", "packing list"), 1),
    ("request_for_quotation", ("demande de prix", "request for quotation", "request for quote", "rfq"), 1),
    ("quotation", ("quotation", "quote", "devis", "angebot"), 1),
    ("contract", ("contract", "contrat", "vertrag", "agreement"), 2),
    ("email", ("subject:", "from:", "to:", "message-id:"), 2),
]


def detect_document_type(text: str, family: str) -> str:
    low = " ".join(text.lower().split())
    title_type = _transaction_title(text)
    if title_type:
        return title_type

    # Purchase orders use a few highly distinctive labels. One strong signal is
    # enough; otherwise combine weaker PO/table signals.
    strong_po = ("purchase order", "bon de commande", "commande fournisseur", "commande achat", "pedido de compra", "bestellung")
    if any(t in low for t in strong_po):
        return "purchase_order"
    po_signals = sum(1 for t in ("po number", "po no", "order date", "customer reference", "supplier", "material", "qty", "grand total") if t in low)
    if po_signals >= 4:
        return "purchase_order"

    best = ("generic_document", 0)
    for name, terms, threshold in DOCUMENT_SIGNATURES:
        hits = sum(1 for t in terms if t in low)
        if hits >= threshold and hits > best[1]:
            best = (name, hits)
    if best[0] != "generic_document":
        return best[0]
    if family == "spreadsheet":
        return "spreadsheet"
    if family == "presentation":
        return "presentation"
    if family == "word":
        return "text_document"
    return "generic_document"



def detect_languages(text: str) -> list[str]:
    low = " " + " ".join(text.casefold().split()) + " "
    vocab = {
        "fr": [" commande ", " fournisseur ", " acheteur ", " livraison ", " désignation ", " quantite ", " quantité ", " prix ", " paiement ", " tva "],
        "de": [" bestellung ", " lieferant ", " lieferung ", " menge ", " preis ", " zahlung ", " rechnung "],
        "en": [" purchase order ", " supplier ", " buyer ", " delivery ", " quantity ", " price ", " payment "],
        "es": [" pedido ", " proveedor ", " entrega ", " cantidad ", " precio ", " pago "],
        "it": [" ordine ", " fornitore ", " consegna ", " quantità ", " prezzo ", " pagamento "],
    }
    scored = [(lang, sum(1 for term in terms if term in low)) for lang, terms in vocab.items()]
    scored = [(lang, score) for lang, score in scored if score >= 2]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [lang for lang, _ in scored[:3]]


def _plausible_phone(value: str) -> bool:
    v = value.strip()
    digits = re.sub(r"\D", "", v)
    if not (7 <= len(digits) <= 15):
        return False
    if v.startswith("+"):
        return True
    separators = sum(ch in " .-()" for ch in v)
    compact = separators == 0
    if compact:
        # Avoid treating ERP/material/order identifiers as phone numbers.
        return len(digits) == 10 and digits.startswith("0")
    # Common local European form (including French 0X XX XX XX XX).
    if digits.startswith("0") and 9 <= len(digits) <= 11 and separators >= 2:
        return True
    # Common North-American grouped form, without requiring a country code.
    if re.fullmatch(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}", v):
        return True
    return False

def extract_key_values(blocks: list[ContentBlock], tables: list[UniversalTable]) -> list[KeyValue]:
    out: list[KeyValue] = []
    seen: set[tuple[str, str]] = set()

    patterns = [
        re.compile(r"^\s*([^:\n]{2,80})\s*:\s*(.{1,500})\s*$"),
        re.compile(r"^\s*([^=\n]{2,80})\s*=\s*(.{1,500})\s*$"),
    ]
    for b in blocks:
        for raw_line in b.text.splitlines() or [b.text]:
            text = " ".join(raw_line.split())
            if not text:
                continue
            for pat in patterns:
                m = pat.match(text)
                if not m:
                    continue
                key, value = m.group(1).strip(), m.group(2).strip()
                if not key or not value:
                    continue
                pair = (key.lower(), value)
                if pair in seen:
                    break
                seen.add(pair)
                out.append(KeyValue(key=key, value=value, raw_value=value, normalized_value=value,
                                    confidence=min(1.0, b.confidence * 0.95), source=b.source,
                                    extraction_method="label_separator"))
                break

    # Two-column tables are often key/value sections.
    for table in tables:
        for r_idx, row in enumerate(table.data, start=1):
            nonempty = [x for x in row if x not in (None, "")]
            if len(nonempty) != 2:
                continue
            key, value = str(nonempty[0]).strip(), str(nonempty[1]).strip()
            if not (2 <= len(key) <= 100 and value):
                continue
            pair = (key.lower(), value)
            if pair in seen:
                continue
            seen.add(pair)
            out.append(KeyValue(key=key, value=value, raw_value=value, normalized_value=value,
                                confidence=table.confidence * 0.90,
                                source=SourceRef(page=table.source.page, sheet=table.source.sheet,
                                                 slide=table.source.slide, row=r_idx),
                                extraction_method="two_column_table"))
    return out[:1000]


def extract_entities(blocks: list[ContentBlock], page_types: dict[int, str] | None = None) -> list[Entity]:
    specs = [
        ("EMAIL", EMAIL_RE, 0.99),
        ("URL", URL_RE, 0.99),
        ("IBAN", IBAN_RE, 0.98),
        ("DATE", DATE_RE, 0.90),
        ("AMOUNT", AMOUNT_RE, 0.90),
        ("PERCENT", PERCENT_RE, 0.95),
        ("PHONE", PHONE_RE, 0.75),
    ]
    out: list[Entity] = []
    seen: set[tuple[str, str, int | None, str | None]] = set()
    for b in blocks:
        for label, pattern, base_conf in specs:
            for m in pattern.finditer(b.text):
                value = m.group(0).strip()
                if label == "PHONE":
                    if not _plausible_phone(value):
                        continue
                    # Company/legal identifiers often look like phone numbers. Reject
                    # candidates whose immediate context identifies them as RCS/SIRET/TVA/etc.
                    a, z = max(0, m.start() - 26), min(len(b.text), m.end() + 26)
                    local = ascii_fold(b.text[a:z]).casefold()
                    if any(tag in local for tag in ("siret", "rcs", "tva", "vat", "iban", "bic", "ape", "capital")):
                        continue
                # Numbered legal clauses such as 13.10 or 6.11 look like decimal
                # amounts to a generic regex. On pages classified as legal terms,
                # require explicit monetary context before emitting AMOUNT.
                if label == "AMOUNT" and page_types and b.source.page and page_types.get(int(b.source.page)) == "legal_terms":
                    # Clause numbers such as 6.12 / 13.10 are section identifiers,
                    # not money, even if the surrounding legal paragraph mentions prices.
                    bare = value.strip()
                    if re.fullmatch(r"\d{1,2}[.]\d{1,2}", bare):
                        continue
                    # For other decimal-looking values on legal pages, require local
                    # monetary evidence next to the matched token rather than anywhere
                    # in a long paragraph block.
                    a, z = max(0, m.start() - 28), min(len(b.text), m.end() + 28)
                    local = ascii_fold(b.text[a:z]).casefold()
                    monetary_context = any(t in local for t in ("eur", "usd", "gbp", "chf", "€", "$", "£", "montant", "prix", "euros", "dollars"))
                    if not monetary_context:
                        continue
                key = (label, value, b.source.page, b.source.sheet)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Entity(text=value, label=label, normalized_value=value,
                                  confidence=min(base_conf, b.confidence), source=b.source))
    return out[:5000]



def extract_structured_key_values(data: dict[str, Any], max_items: int = 2000) -> list[KeyValue]:
    out: list[KeyValue] = []

    def walk(value: Any, path: str = "") -> None:
        if len(out) >= max_items:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                p = f"{path}.{k}" if path else str(k)
                walk(v, p)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        elif value is not None and path:
            out.append(KeyValue(key=path, value=value, raw_value=str(value), normalized_value=value,
                                confidence=1.0, source=SourceRef(), extraction_method="structured_data"))

    walk(data)
    return out

def synthetic_pages(blocks: list[ContentBlock], tables: list[UniversalTable]) -> list[PageResult]:
    """Create layout-like pages for semantic business extractors when a file has no pages.

    Coordinates are synthetic but deterministic; they preserve line and table order.
    """
    lines: list[tuple[int, str, float]] = []
    sequence = 0
    for b in blocks:
        if b.text.strip():
            sequence += 1
            lines.append((sequence, b.text.strip(), b.confidence))
    for table in tables:
        for row in table.data:
            text = " ".join(str(x) for x in row if x not in (None, ""))
            if text.strip():
                sequence += 1
                lines.append((sequence, text.strip(), table.confidence))

    words: list[WordToken] = []
    texts: list[str] = []
    y = 10.0
    for _, line, conf in lines:
        texts.append(line)
        x = 10.0
        for token in line.split():
            w = max(8.0, len(token) * 6.0)
            words.append(WordToken(text=token, page=1, bbox=(x, y, x + w, y + 10.0),
                                   confidence=conf, source="structured_file"))
            x += w + 5.0
        y += 16.0
    if not words:
        return []
    return [PageResult(page=1, width=max(1000.0, max(w.bbox[2] for w in words) + 20),
                       height=max(1000.0, y + 20), source_type="structured_file",
                       text="\n".join(texts), words=words,
                       confidence=statistics.fmean(w.confidence for w in words))]

# --- V4.2 page-aware routing -------------------------------------------------

def _contains_any(low: str, terms: tuple[str, ...] | list[str]) -> list[str]:
    return [t for t in terms if t in low]


def _transaction_title(text: str) -> str | None:
    """Find an anchored document heading, not a document mentioned in prose.

    Number/reference labels further down an invoice or quotation must not turn it
    into a purchase order. The first explicit heading in the header wins.
    """
    number = r"(?:n[\u00b0\u00bao]?|no\.?|num(?:ero)?|number|#)"
    folded_header = ascii_fold(text[:1800]).casefold()
    folded_page = ascii_fold(text[:12000]).casefold()
    remittance_markers = sum(
        marker in folded_page
        for marker in (
            "virement bancaire",
            "total virement",
            "date de valeur",
            "reglement du releve",
            "libelle date votre ref. notre ref. montant",
        )
    )
    if (
        "remittance advice" in folded_page
        or "avis de virement" in folded_page
        or remittance_markers >= 3
        or (
            "total virement" in folded_page
            and "date de valeur" in folded_page
            and len(re.findall(r"(?m)^\s*(?:factf|avoif)\b", folded_page)) >= 2
        )
    ):
        return "remittance_advice"
    if re.search(
        r"\b(?:demande\s+d[' ]?annulation\s+d[' ]?une\s+commande|"
        r"annulation\s+de\s+commande|order\s+cancellation)\b",
        folded_header,
    ):
        return "order_cancellation"
    patterns = (
        ("order_cancellation", r"^(?:demande\s+d[' ]?annulation\s+d[' ]?une\s+commande|annulation\s+de\s+commande|order\s+cancellation)\b"),
        ("approval_form", r"^(?:rb\s+)?general\s+approval\s+form\b"),
        ("generic_document", r"^(?:relance\s+retour(?:s|\(s\))?\s+sans\s+a\.?r\.?|relance\s+commande(?:s|\(s\))?\s+sans\s+a\.?r\.?|notification\s+de\s+relance\s+d[' ]une\s+commande|relance\s+(?:d[' ]?)?accuse\s+de\s+reception|(?:abandon|annulation)\s+(?:de\s+)?reliquat)\b"),
        ("order_confirmation", r"^(?:confirmation\s+(?:de\s+)?commande|order\s+confirmation|auftragsbestatigung)\b"),
        ("request_for_quotation", r"^(?:demande\s+de\s+prix|request\s+for\s+(?:quotation|quote)|rfq)\b"),
        ("quotation", r"^(?:offre\s+(?:de\s+prix|commerciale)|devis|quotation|quote|angebot)\b"),
        ("invoice", r"^(?:facture|invoice|rechnung)\b"),
        ("delivery_note", r"^(?:bon\s+de\s+livraison|delivery\s+note|packing\s+list|lieferschein)\b"),
        ("purchase_order", rf"^(?:bon\s+de\s+commande|purchase\s+order|commande\s+(?:fournisseur|achat|regroupee)|pedido\s+de\s+compra|bestellung)\b|^commande\s+{number}(?:\s|[:.\u00b0\u00ba]|\d|$)|^c\s+o\s+m\s+m\s+a\s+n\s+d\s+e(?:\s|_|$)"),
    )
    for line in text[:1800].splitlines():
        heading = " ".join(ascii_fold(line).casefold().split())
        if not heading:
            continue
        if re.match(r"^(?:conditions generales|general terms and conditions|conditions of purchase|allgemeine einkaufsbedingungen)\b", heading):
            # A transactional form can merely point to its online purchasing
            # terms. A URL on the same line is a reference, not the title of
            # a standalone legal-terms document.
            if re.search(r"\b(?:www\.|https?://)", heading):
                continue
            return None
        for kind, pattern in patterns:
            if not re.search(pattern, heading):
                continue
            # "Facture a expedier", "devis valable ..." and similar labels are
            # routing instructions or conditions, not document headings.
            if re.match(r"^(?:facture\s+a\b|devis\s+(?:valable|complementaire)\b)", heading):
                continue
            return kind
        # OCR can concatenate the issuer's letterhead and a title printed in
        # another column. Keep a terminal uppercase title as title evidence.
        # Requiring the phrase at the end avoids ordinary mentions in prose.
        if re.search(r"\b(?:COMMANDE FOURNISSEUR|COMMANDE ACHAT|BON DE COMMANDE|PURCHASE ORDER)\s*$", ascii_fold(line)):
            return "purchase_order"
        if re.search(r"\bCOMMANDE\s+REGROUPEE\b", ascii_fold(line), flags=re.I):
            return "purchase_order"
        inline_order = re.search(
            r"\bCOMMANDE\s+N(?:O|UMERO)?[^A-Z0-9\n]{0,3}[A-Z0-9][A-Z0-9._/\-]{3,}",
            ascii_fold(line),
            flags=re.I,
        )
        if inline_order and not re.search(
            r"\bBON\s+DE\s+$",
            ascii_fold(line)[:inline_order.start()],
            flags=re.I,
        ):
            return "purchase_order"
        # Native PDF extraction can interleave the value printed below the
        # title with the title itself, for example ``ST COMMANDE T CSP N°
        # 41479`` for a visual ``COMMANDE N° / ST T CSP 41479`` block.  The
        # combination of an in-header number marker and a substantial numeric
        # identifier is still explicit order-title evidence.  Exclude the
        # common non-order transactional headings before applying the repair.
        folded_line = ascii_fold(line)
        interleaved_order = re.search(
            r"\bCOMMANDE\b[^\n]{0,40}\bN(?:O|UMERO)?[^A-Z0-9\n]{0,3}",
            folded_line,
            flags=re.I,
        )
        if (
            interleaved_order
            and re.search(r"\b\d{4,}\b", folded_line)
            and not re.search(
                r"\bBON\s+DE\s+$",
                folded_line[:interleaved_order.start()],
                flags=re.I,
            )
            and not re.search(
                r"\b(?:CONFIRMATION|ANNULATION|RELANCE|ACCUSE\s+DE\s+RECEPTION)\b",
                folded_line,
                flags=re.I,
            )
        ):
            return "purchase_order"
        # Column extraction can splice the PAGE heading between DE and PRIX.
        # This remains an explicit request-for-price title, not a purchase order.
        if re.search(r"\bDEMANDE\s+DE(?:\s+PAGE)?\s+PRIX\b", ascii_fold(line), flags=re.I):
            return "request_for_quotation"
        if re.search(r"\b(?:ABANDON|ANNULATION)\s+(?:DE\s+)?RELIQUAT\s*$", ascii_fold(line)):
            return "generic_document"
    return None


def classify_page(text: str, *, family: str = "pdf", page_number: int | None = None) -> dict[str, Any]:
    """Classify one page independently with weighted, explainable signatures.

    Page-level routing prevents appended legal terms, certificates or annexes from
    overriding the semantic type of the transactional page that precedes them.
    """
    low = " ".join(ascii_fold(text).casefold().split())
    title_type = _transaction_title(text)
    folded_lines = [" ".join(ascii_fold(line).casefold().split()) for line in text.splitlines()]
    scores: dict[str, float] = {
        "order_cancellation": 0.0,
        "remittance_advice": 0.0,
        "approval_form": 0.0,
        "order_confirmation": 0.0,
        "purchase_order": 0.0,
        "legal_terms": 0.0,
        "invoice": 0.0,
        "delivery_note": 0.0,
        "request_for_quotation": 0.0,
        "quotation": 0.0,
        "contract": 0.0,
        "generic_document": 0.25,
    }
    signals: dict[str, list[str]] = {k: [] for k in scores}

    def add(kind: str, points: float, signal: str) -> None:
        scores[kind] = scores.get(kind, 0.0) + points
        signals.setdefault(kind, []).append(signal)

    # Purchase-order signatures. Header + line table structure receives more
    # weight than a generic occurrence of the word "commande" in legal prose.
    for term, pts in [
        ("purchase order", 7.0), ("bon de commande", 7.0),
        ("commande regroupee", 8.0),
        ("commande fournisseur", 7.0), ("commande achat", 7.0),
        ("pedido de compra", 7.0), ("bestellung", 5.0),
        ("n° commande", 6.0), ("nº commande", 6.0), ("no commande", 5.0),
        ("commande n°", 6.0), ("commande nº", 6.0), ("commande no", 5.0),
        ("po number", 5.0), ("po no", 4.0),
    ]:
        if term in low:
            add("purchase_order", pts, term)
    po_headers = [
        "code article", "libelle", "designation", "commande", "quantite", "quantity",
        "pu h.t", "pu ht", "unit price", "prix unitaire", "n° client", "adresse de livraison",
        "reference fournisseur", "sem/ann", "delai", "montant", "net h.t",
        "total h.t", "total ht", "net a payer", "grand total",
    ]
    header_hits = _contains_any(low, po_headers)
    if header_hits:
        add("purchase_order", min(8.5, len(header_hits) * 1.15), "headers:" + ",".join(header_hits[:9]))
    # Strong ERP line-table signature, robust to repeated headers on continuation pages.
    erp_hits = _contains_any(low, ["designation", "reference fournisseur", "quantite", "sem/ann", "montant", "net h.t"])
    if len(erp_hits) >= 4:
        add("purchase_order", 4.0, "erp-line-table:" + ",".join(erp_hits[:6]))
    # A short transactional first page with COMMANDE + numeric lines is a useful
    # signal, but intentionally weaker than the explicit header phrases above.
    if re.search(r"\bcommande\b", low[:1800]):
        add("purchase_order", 1.2, "commande-near-top")
    if re.search(r"\b[a-z]{0,4}\s*\d{6,14}\b", low) and any(x in low for x in ("pu h.t", "quantity", "commande", "code article")):
        add("purchase_order", 1.2, "item-like-identifiers")

    for term in ("confirmation de commande", "confirmation commande", "order confirmation", "auftragsbestatigung"):
        if term in low:
            add("order_confirmation", 7.0, term)

    # Legal terms / purchase conditions.
    legal_heading = any(
        re.match(
            r"^(?:conditions generales|general terms and conditions|conditions of purchase|allgemeine einkaufsbedingungen)\b",
            line,
        )
        and not re.search(r"\b(?:www\.|https?://)", line)
        for line in folded_lines
    )
    for term, pts in [
        ("conditions generales d’achat", 10.0), ("conditions generales d'achat", 10.0),
        ("general terms and conditions", 9.0), ("conditions of purchase", 8.0),
        ("allgemeine einkaufsbedingungen", 9.0), ("conditions generales", 4.0),
    ]:
        if term in low:
            # A footer referring to terms is not itself a page of legal terms.
            add("legal_terms", pts if legal_heading else min(pts, 1.5), term)
    # Clause numbers are layout markers, not arbitrary numbers followed by a
    # word. Searching the whitespace-flattened page used to count addresses,
    # dates and quantities (for example ``42 RUE`` or ``1 PCE``) as legal
    # articles. On short purchase-order pages that false legal score could
    # trigger the anti-legal guard and divide the PO score by four. Real legal
    # clauses start on their own extracted line, so retain the line boundary.
    article_markers = [
        match.group(1)
        for line in folded_lines
        if (match := re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+[a-zà-ÿ]", line))
        and int(match.group(1).split(".", 1)[0]) <= 20
    ]
    if len(article_markers) >= 4:
        add("legal_terms", min(7.0, 1.0 + len(article_markers) * 0.35), f"numbered-clauses:{len(article_markers)}")
    legal_vocab = _contains_any(low, [
        "acquereur", "fournisseur", "resiliation", "propriete intellectuelle", "confidentialite",
        "responsabilite", "litiges", "droit applicable", "penalites de retard", "protection des donnees",
    ])
    if len(legal_vocab) >= 3:
        add("legal_terms", min(5.0, len(legal_vocab) * 0.75), "legal-vocab:" + ",".join(legal_vocab[:6]))

    # Other common document types.
    for kind, terms in {
        "order_cancellation": [("demande d'annulation d'une commande", 10.0), ("annulation de commande", 8.0), ("order cancellation", 8.0)],
        "remittance_advice": [("remittance advice", 10.0), ("avis de virement", 9.0), ("total virement", 5.0), ("date de valeur", 3.0)],
        "approval_form": [("rb general approval form", 9.0), ("general approval form", 7.0), ("approval workflow", 4.0), ("approver(s)", 2.0)],
        "invoice": [("facture", 4.0), ("invoice", 4.0), ("amount due", 2.0), ("total ttc", 2.0)],
        "delivery_note": [("bon de livraison", 6.0), ("delivery note", 6.0), ("packing list", 4.0)],
        "request_for_quotation": [("demande de prix", 7.0), ("request for quotation", 7.0), ("request for quote", 7.0), ("rfq", 5.0)],
        "quotation": [("devis", 5.0), ("quotation", 5.0), ("quote", 3.0), ("angebot", 4.0)],
        "contract": [("contrat", 3.0), ("contract", 3.0), ("agreement", 3.0), ("vertrag", 3.0)],
    }.items():
        for term, pts in terms:
            if term in low:
                add(kind, pts, term)

    if title_type:
        add(title_type, 12.0, "explicit-title:" + title_type)
        signals[title_type].insert(0, signals[title_type].pop())
        if title_type != "purchase_order":
            # Tables, delivery addresses and order references also occur on
            # invoices and quotations; a real heading is stronger evidence.
            scores["purchase_order"] *= 0.25
    elif scores["legal_terms"] >= 3.0:
        # Legal prose often mentions "bon de commande". Without a heading or
        # genuine numeric line-table structure, it must not become a PO.
        table_groups = (
            r"\b(?:quantite|quantity|qte|qty)\b",
            r"\b(?:designation|description|reference|code article)\b",
            r"\b(?:prix|price|montant|amount|pu)\b",
        )
        if not all(re.search(pattern, low) for pattern in table_groups):
            scores["purchase_order"] *= 0.25

    # Legal pages often mention orders/contracts. Explicit CGA/T&C evidence wins
    # unless the same page also has strong transactional PO headers.
    if scores["legal_terms"] >= 8.0 and scores["purchase_order"] < 9.0:
        scores["purchase_order"] *= 0.35
    if scores["purchase_order"] >= 8.0 and scores["legal_terms"] < 8.0:
        scores["contract"] *= 0.5

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    winner, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence combines absolute evidence and separation from the runner-up.
    absolute = min(1.0, top / 10.0)
    margin = 0.0 if top <= 0 else max(0.0, min(1.0, (top - second) / max(top, 1.0)))
    confidence = round(max(0.35, min(0.995, 0.62 * absolute + 0.38 * margin)), 4)
    if top < 1.5:
        winner = "generic_document"
        confidence = 0.45
    return {
        "page": page_number,
        "type": winner,
        "confidence": confidence,
        "explicit_document_title": title_type,
        "scores": {k: round(v, 3) for k, v in scores.items() if v > 0.25 or k == winner},
        "signals": signals.get(winner, [])[:12],
    }


def aggregate_page_classifications(page_classifications: list[dict[str, Any]], *, fallback_text: str = "", family: str = "pdf") -> dict[str, Any]:
    """Aggregate page types into a primary type and composite document type."""
    if not page_classifications:
        dt = detect_document_type(fallback_text, family)
        return {
            "detected_document_type": dt,
            "primary_document_type": dt,
            "is_composite": False,
            "confidence": 0.65,
            "components": [{"type": dt, "pages": [], "confidence": 0.65}],
        }

    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in page_classifications:
        by_type.setdefault(str(item.get("type", "generic_document")), []).append(item)

    # Transactional pages take precedence over appended legal/supporting pages.
    transactional_priority = [
        "order_cancellation", "remittance_advice", "order_confirmation", "purchase_order", "invoice", "delivery_note",
        "request_for_quotation", "quotation",
    ]
    primary = None
    for kind in transactional_priority:
        if kind in by_type:
            primary = kind
            break
    if primary is None:
        primary = max(by_type.items(), key=lambda kv: sum(float(x.get("confidence", 0.0)) for x in kv[1]))[0]

    non_generic = [k for k in by_type if k != "generic_document"]
    is_composite = len(non_generic) > 1
    detected = primary
    if primary == "purchase_order" and "legal_terms" in by_type:
        detected = "purchase_order_with_terms"
    elif primary == "purchase_order" and is_composite:
        detected = "purchase_order_bundle"

    components = []
    for kind, items in sorted(by_type.items(), key=lambda kv: min((x.get("page") or 999999) for x in kv[1])):
        components.append({
            "type": kind,
            "pages": [int(x["page"]) for x in items if x.get("page") is not None],
            "confidence": round(sum(float(x.get("confidence", 0.0)) for x in items) / max(1, len(items)), 4),
        })
    primary_items = by_type.get(primary, [])
    conf = round(sum(float(x.get("confidence", 0.0)) for x in primary_items) / max(1, len(primary_items)), 4)
    return {
        "detected_document_type": detected,
        "primary_document_type": primary,
        "is_composite": is_composite,
        "confidence": conf,
        "components": components,
    }


def classify_pages(page_payloads: list[dict[str, Any]] | None, page_results: list[Any] | None, *, family: str = "pdf") -> list[dict[str, Any]]:
    """Create page-level classifications from either generic payloads or PageResult objects."""
    source: list[tuple[int, str]] = []
    if page_results:
        source = [(int(getattr(p, "page", i + 1)), str(getattr(p, "text", "") or "")) for i, p in enumerate(page_results)]
    elif page_payloads:
        for i, p in enumerate(page_payloads):
            page_no = p.get("page") or p.get("slide") or (i + 1)
            source.append((int(page_no), str(p.get("text", "") or "")))
    classifications = [classify_page(text, family=family, page_number=page) for page, text in source]
    explicit_types = {item["explicit_document_title"] for item in classifications if item.get("explicit_document_title")}
    if len(explicit_types) == 1:
        document_type = next(iter(explicit_types))
        if document_type != "purchase_order":
            # An untitled product table inherits an unambiguous invoice/quote
            # heading. Preserve independently titled orders in mixed bundles.
            for item in classifications:
                if item["type"] == "purchase_order" and not item.get("explicit_document_title"):
                    item["type"] = document_type
                    item["confidence"] = min(item["confidence"], 0.84)
                    item.setdefault("signals", []).append("document-title-context:" + document_type)
    return classifications
