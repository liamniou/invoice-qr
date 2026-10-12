"""Gradio app: upload a Swedish invoice PDF, get a bank-scannable payment QR code.

The QR payload follows the Swedish Bankers' Association ("Svenska Bankforeningen")
unstructured invoice QR format, which Swedish bank apps recognise to pre-fill a
bankgiro/plusgiro payment.
"""

import json
import re
import tempfile
from pathlib import Path

import gradio as gr
import pdfplumber
import qrcode

# --- Limits on untrusted input -------------------------------------------------
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PAGES = 5

# Company-form suffixes used to cut the payee name out of the footer line.
COMPANY_SUFFIXES = {"AB", "HB", "KB", "AKTIEBOLAG", "EK.FOR."}

DATE_RE = r"\d{4}-\d{2}-\d{2}"
# Swedish amounts: "14 875,00" / "1 625,00" / "1625,00". The thousands
# separator may be a (narrow) no-break space or a dot.
AMOUNT_RE = r"\d{1,3}(?:[ \u00a0\u2009\u202f.]\d{3})+,\d{2}|\d+,\d{2}"

# Label synonyms per field, most specific first. Matched case-insensitively.
AMOUNT_LABELS = [
    r"Totalt\s+belopp", r"Summa\s+att\s+betala", r"Totalt\s+att\s+betala",
    r"Belopp\s+att\s+betala", r"Att\s+betala", r"Totalbelopp", r"Totalt",
]
INVOICE_NO_LABELS = [r"Faktura\s*-?\s*n(?:umme)?r\.?"]
INVOICE_DATE_LABELS = [
    r"Fakturadatum", r"Faktureringsdatum", r"Fakturadag", r"Datum",
]
DUE_DATE_LABELS = [
    r"F\u00f6rfallodatum", r"F\u00f6rfallodag", r"F\u00f6rfaller",
    r"Sista\s+betalningsdag(?:en)?", r"(?:Att\s+)?betalas?\s+senast",
]
OCR_LABELS = [
    r"OCR\s*-?\s*(?:nummer|nr\.?|referens)", r"OCR",
    r"Betalningsreferens", r"Referensnummer",
]
ORG_LABELS = [
    r"Organisations?\s*-?\s*n(?:umme)?r\.?",
    r"Org\.?\s*-?\s*n(?:umme)?r\.?",
]
VAT_LABELS = [
    r"Momsreg(?:istrerings)?\.?\s*-?\s*n(?:umme)?r\.?",
    r"VAT\s*-?\s*(?:nr|no|nummer|number)?\.?", r"Moms\s*nr\.?",
]
BANKGIRO_LABELS = [r"Bankgiro(?:nummer|nr)?", r"BG(?:\s*-?\s*nr)?"]
PLUSGIRO_LABELS = [r"Plusgiro(?:nummer|nr)?", r"Postgiro", r"PG(?:\s*-?\s*nr)?"]
# Labels under/after which the seller's name is printed.
PAYEE_LABELS = [
    r"Betalningsmottagare", r"Leverant\u00f6r", r"S\u00e4ljare",
    r"F\u00f6retagsnamn", r"F\u00f6retag",
]
ADDRESS_LABELS = [r"Postadress", r"Adress"]


def parse_amount(raw):
    """'14 875,00' -> 14875.0"""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\u00a0\u2009\u202f.]", "", raw).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def luhn_ok(number):
    """Swedish OCR references carry a Luhn (mod-10) check digit."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 2:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _make_line(words, page_no):
    words.sort(key=lambda w: w["x0"])
    spans, text = [], ""
    for word in words:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(word["text"])))
        text += word["text"]
    return {
        "page": page_no,
        "text": text,
        "words": words,
        "spans": spans,
        "top": min(w["top"] for w in words),
        "bottom": max(w["bottom"] for w in words),
    }


def extract_lines(pdf_path):
    """Return text lines that also keep each word's position on the page."""
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages[:MAX_PAGES]):
            words = page.extract_words(x_tolerance=1.5, y_tolerance=3)
            words.sort(key=lambda w: (w["top"], w["x0"]))
            current, current_top = [], None
            for word in words:
                if current and abs(word["top"] - current_top) > 3:
                    lines.append(_make_line(current, page_no))
                    current = []
                if not current:
                    current_top = word["top"]
                current.append(word)
            if current:
                lines.append(_make_line(current, page_no))
    return lines


def _joined(a, b):
    """True if word b follows word a with a normal space, not a column gap."""
    height = max(a["bottom"] - a["top"], b["bottom"] - b["top"])
    return b["x0"] - a["x1"] < 0.6 * height


def _column_text(line, x0, x1):
    """Text of the word run in `line` sitting under the horizontal span x0..x1.

    Neighbouring words are joined while the gap between them looks like a
    normal space; a wider gap is treated as a column boundary.
    """
    words = line["words"]
    hits = [i for i, w in enumerate(words) if w["x0"] < x1 and w["x1"] > x0]
    if not hits:
        return None

    first, last = hits[0], hits[-1]
    while first > 0 and _joined(words[first - 1], words[first]):
        first -= 1
    while last < len(words) - 1 and _joined(words[last], words[last + 1]):
        last += 1
    return " ".join(w["text"] for w in words[first:last + 1])


def find_value(lines, labels, value_re, free_text=False):
    """Find the value printed after a label, or in the same column below it.

    Returns the regex match object of `value_re` (anchored at the value start).
    Labels are tried in priority order across the whole document. With
    `free_text`, a same-line value must directly follow the label (no column
    gap), so a header row of labels is not mistaken for a value.
    """
    for label in labels:
        label_re = re.compile(rf"(?<!\w)(?:{label})(?!\w)", re.IGNORECASE)
        for idx, line in enumerate(lines):
            for found in label_re.finditer(line["text"]):
                label_idx = [
                    i for i, (s, e) in enumerate(line["spans"])
                    if s < found.end() and e > found.start()
                ]
                label_words = [line["words"][i] for i in label_idx]
                rest = line["text"][found.end():]
                if free_text:
                    words = line["words"]
                    end = label_idx[-1]
                    while end + 1 < len(words) and _joined(words[end], words[end + 1]):
                        end += 1
                    rest = line["text"][found.end():line["spans"][end][1]]
                rest = re.sub(r"^\s*:?\s*", "", rest)
                same_line = re.match(value_re, rest, re.IGNORECASE)
                if same_line:
                    return same_line

                x0 = min(w["x0"] for w in label_words)
                x1 = max(w["x1"] for w in label_words)
                for below in lines[idx + 1: idx + 4]:
                    if (below["page"] != line["page"]
                            or below["top"] - line["bottom"] > 40):
                        break
                    text = _column_text(below, x0, x1)
                    if text is None:
                        continue
                    value = re.match(value_re, text, re.IGNORECASE)
                    if value:
                        return value
                    break
    return None


def find_group(lines, labels, value_re):
    found = find_value(lines, labels, value_re)
    return found.group(1) if found else None


def _cut_at_company_suffix(text):
    """'Acme Sverige AB 08-123 45' -> 'Acme Sverige AB'; None if no suffix."""
    tokens = text.split()
    for i, token in enumerate(tokens):
        if token.strip(".,").upper() in COMPANY_SUFFIXES:
            return " ".join(tokens[:i] + [token.rstrip(",")])
    return None


def payee_name(lines):
    """Find the seller's company name."""
    # 1. Footer line "Acme AB Organisationsnummer: 556..." - name precedes it.
    for label in ORG_LABELS:
        label_re = re.compile(
            rf"(?<!\w)(?:{label})(?!\w)\s*:?\s*\d{{6}}-?\d{{4}}", re.IGNORECASE
        )
        for line in lines:
            found = label_re.search(line["text"])
            if found:
                head = line["text"][:found.start()].strip(" :,")
                if re.search(r"[A-Za-z\u00c0-\u00ff]{2}", head):
                    return _cut_at_company_suffix(head) or head

    # 2. An explicit payee label ("Betalningsmottagare: Acme AB").
    found = find_value(lines, PAYEE_LABELS, r"(\S.*)", free_text=True)
    if found:
        text = found.group(1).strip()
        return _cut_at_company_suffix(text) or text

    # 3. A footer "Adress" column whose first line is the company name.
    found = find_value(lines, ADDRESS_LABELS, r"(\S.*)", free_text=True)
    if found:
        name = _cut_at_company_suffix(found.group(1))
        if name and not name.lower().startswith("c/o"):
            return name

    # 4. Last resort: the lowest line carrying a company-form suffix
    # (sellers usually sit in the footer, customers in the header).
    for line in reversed(lines):
        if line["text"].lower().startswith("c/o"):
            continue
        name = _cut_at_company_suffix(line["text"])
        if name:
            return name
    return None


def org_number(lines):
    org = find_group(lines, ORG_LABELS, r"(\d{6}-?\d{4})")
    if org:
        return org
    # Swedish VAT numbers are SE + org. number + 01.
    vat = find_value(lines, VAT_LABELS, r"SE\s?(\d{6})-?(\d{4})\s?01")
    if vat:
        return f"{vat.group(1)}-{vat.group(2)}"
    return None


def parse_invoice_pdf(pdf_path):
    """Return (fields, warnings) extracted from a Swedish invoice PDF."""
    size = Path(pdf_path).stat().st_size
    if size > MAX_PDF_BYTES:
        raise gr.Error(f"PDF is too large ({size / 1e6:.1f} MB, limit 10 MB).")

    with open(pdf_path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise gr.Error("That file is not a PDF.")

    try:
        lines = extract_lines(pdf_path)
    except Exception as exc:  # noqa: BLE001 - surface any parser failure in the UI
        raise gr.Error(f"Could not read the PDF: {exc}") from exc

    if not lines:
        raise gr.Error(
            "No text found in the PDF - it is probably a scan. "
            "This app needs a text-based invoice."
        )

    amount_raw = find_group(
        lines, AMOUNT_LABELS, rf"(?:SEK|kr)?\s*({AMOUNT_RE})(?!\d)"
    )

    # Invoice number and date may sit in a "Fakturanummer / Datum / ..." table
    # row; otherwise look them up by label.
    invoice_no = None
    invoice_date = None
    for idx, line in enumerate(lines):
        if (re.search(r"Fakturanummer\s+Datum", line["text"])
                and idx + 1 < len(lines)):
            row = re.match(rf"^(\S+)\s+({DATE_RE})", lines[idx + 1]["text"])
            if row:
                invoice_no, invoice_date = row.group(1), row.group(2)
            break
    if invoice_no is None:
        invoice_no = find_group(
            lines, INVOICE_NO_LABELS, r"(?=[A-Za-z-]*\d)([A-Za-z0-9-]+)"
        )
    if invoice_date is None:
        invoice_date = find_group(lines, INVOICE_DATE_LABELS, rf"({DATE_RE})")

    giro = find_group(lines, BANKGIRO_LABELS, r"(\d{3,4}-\d{4})(?!\d)")
    giro_type = "BG"
    if giro is None:
        plusgiro = find_group(lines, PLUSGIRO_LABELS, r"(\d[\d ]{0,9}-\d)(?!\d)")
        if plusgiro:
            giro, giro_type = plusgiro.replace(" ", ""), "PG"

    fields = {
        "payee_name": payee_name(lines),
        "org_number": org_number(lines),
        "ocr_number": find_group(lines, OCR_LABELS, r"(\d{2,25})(?!\d)"),
        "invoice_date": invoice_date,
        "due_date": find_group(lines, DUE_DATE_LABELS, rf"({DATE_RE})"),
        "amount": parse_amount(amount_raw),
        "giro": giro,
        "giro_type": giro_type,
        "invoice_no": invoice_no,
    }

    labels = {
        "payee_name": "payee name",
        "org_number": "organisation number",
        "ocr_number": "OCR number",
        "invoice_date": "invoice date",
        "due_date": "due date",
        "amount": "total amount",
        "giro": "bankgiro/plusgiro number",
    }
    warnings = [
        f"Could not find the {label} - please fill it in manually."
        for key, label in labels.items()
        if not fields.get(key)
    ]

    ocr = fields.get("ocr_number")
    if ocr and not luhn_ok(ocr):
        warnings.append(
            f"OCR number {ocr} fails its check digit - verify it before paying."
        )

    return fields, warnings


def build_payload(name, org_number, ocr_number, invoice_date, due_date,
                  amount, giro, giro_type):
    return {
        "uqr": 1,                                            # QR format version
        "tp": 1,                                             # 1 = invoice
        "nme": (name or "").strip(),                         # Payee name
        "cid": (org_number or "").replace("-", "").strip(),  # Org. number, no dash
        "iref": (ocr_number or "").strip(),                  # Invoice reference (OCR)
        "idt": (invoice_date or "").replace("-", ""),        # Invoice date YYYYMMDD
        "ddt": (due_date or "").replace("-", ""),            # Due date YYYYMMDD
        "due": round(float(amount or 0), 2),                 # Amount due, SEK
        "pt": giro_type or "BG",                             # BG bankgiro / PG plusgiro
        "acc": (giro or "").strip(),                         # Giro number
    }


def render_qr(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4
    )
    code.add_data(data)
    code.make(fit=True)
    img = code.make_image(fill_color="black", back_color="white").convert("RGB")

    slug = re.sub(r"[^A-Za-z0-9]+", "-", payload.get("iref") or "invoice").strip("-")
    out = Path(tempfile.mkdtemp()) / f"invoice-qr-{slug or 'invoice'}.png"
    img.save(out)
    return img, data, str(out)


def format_swedish_amount(amount):
    """1625.0 -> '1 625,00' (space thousands separator, comma decimal)."""
    return f"{amount:,.2f}".replace(",", "\u00a0").replace(".", ",")


def format_summary(fields, warnings):
    amount = fields.get("amount")
    amount_text = format_swedish_amount(amount) if amount else "-"
    parts = [
        f"### {fields.get('payee_name') or 'Unknown payee'}",
        f"- Amount: **{amount_text} SEK**",
        f"- Due: **{fields.get('due_date') or '-'}**",
        f"- {fields.get('giro_type') or 'BG'}: **{fields.get('giro') or '-'}**",
        f"- OCR: **{fields.get('ocr_number') or '-'}**",
    ]
    if fields.get("invoice_no"):
        parts.append(f"- Invoice no: {fields['invoice_no']}")
    if warnings:
        parts.append("")
        parts.extend(f"**Check:** {w}" for w in warnings)
    return "\n".join(parts)


def on_upload(pdf_file):
    """Parse the PDF, fill the form and render the QR in one step."""
    if not pdf_file:
        return "", "", "", "", "", None, "", "BG", None, "", None, ""

    fields, warnings = parse_invoice_pdf(pdf_file)
    form = (
        fields.get("payee_name") or "",
        fields.get("org_number") or "",
        fields.get("ocr_number") or "",
        fields.get("invoice_date") or "",
        fields.get("due_date") or "",
        fields.get("amount"),
        fields.get("giro") or "",
        fields.get("giro_type") or "BG",
    )
    summary = format_summary(fields, warnings)

    # Without a giro number and an amount there is nothing payable to encode.
    if not (fields.get("giro") and fields.get("amount")):
        return (*form, None, "", None, summary)

    payload = build_payload(
        fields["payee_name"], fields["org_number"], fields["ocr_number"],
        fields["invoice_date"], fields["due_date"], fields["amount"],
        fields["giro"], fields["giro_type"],
    )
    img, data, path = render_qr(payload)
    return (*form, img, data, path, summary)


def on_generate(name, org_number, ocr_number, invoice_date, due_date,
                amount, giro, giro_type):
    if not giro:
        raise gr.Error("A bankgiro or plusgiro number is required.")
    if not amount:
        raise gr.Error("An amount is required.")
    payload = build_payload(name, org_number, ocr_number, invoice_date,
                            due_date, amount, giro, giro_type)
    return render_qr(payload)


with gr.Blocks(title="Swedish Invoice QR") as demo:
    gr.Markdown(
        "# Swedish Invoice QR\n"
        "Upload an invoice PDF - the payment details are read automatically and "
        "turned into a QR code you can scan in your Swedish bank app.\n\n"
        "Always check the amount, OCR and giro number against the invoice before paying."
    )

    with gr.Row():
        with gr.Column(scale=1):
            pdf_in = gr.File(
                label="Invoice PDF",
                file_types=[".pdf"],
                file_count="single",
                type="filepath",
            )
            summary_out = gr.Markdown()

            with gr.Accordion("Extracted fields (edit if needed)", open=False):
                payee_in = gr.Textbox(label="Payee name")
                org_in = gr.Textbox(label="Organisation number")
                ocr_in = gr.Textbox(label="OCR number")
                idate_in = gr.Textbox(label="Invoice date (YYYY-MM-DD)")
                ddate_in = gr.Textbox(label="Due date (YYYY-MM-DD)")
                amount_in = gr.Number(label="Amount (SEK)")
                giro_in = gr.Textbox(label="Bankgiro / Plusgiro")
                giro_type_in = gr.Radio(["BG", "PG"], value="BG", label="Giro type")
                regen_btn = gr.Button("Regenerate QR", variant="secondary")

        with gr.Column(scale=1):
            qr_out = gr.Image(label="Scan with your bank app", type="pil")
            png_out = gr.File(label="Download PNG")
            payload_out = gr.Textbox(label="QR payload (JSON)", lines=4)

    form_fields = [payee_in, org_in, ocr_in, idate_in, ddate_in,
                   amount_in, giro_in, giro_type_in]

    pdf_in.change(
        on_upload,
        inputs=[pdf_in],
        outputs=[*form_fields, qr_out, payload_out, png_out, summary_out],
    )
    regen_btn.click(
        on_generate,
        inputs=form_fields,
        outputs=[qr_out, payload_out, png_out],
    )


if __name__ == "__main__":
    demo.launch()
