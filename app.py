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
# Swedish amounts: "14 875,00" / "1 625,00". The thousands space may be NBSP.
AMOUNT_RE = r"\d[\d\s\u00a0\u2009\u202f]*,\d{2}"


def parse_amount(raw):
    """'14 875,00' -> 14875.0"""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\u00a0\u2009\u202f]", "", raw).replace(",", ".")
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


def extract_lines(pdf_path):
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            text = page.extract_text(x_tolerance=1.5) or ""
            lines.extend(line.strip() for line in text.split("\n") if line.strip())
    return lines


def first_match(lines, pattern, group=1):
    for line in lines:
        found = re.search(pattern, line)
        if found:
            return found.group(group)
    return None


def payee_name(lines):
    """Pull the company name off the footer line that carries the org. number."""
    for line in lines:
        if "Organisationnummer" in line:
            head = line.split("Organisationnummer")[0].strip()
            tokens = head.split()
            for i, token in enumerate(tokens):
                if token.strip(".,").upper() in COMPANY_SUFFIXES:
                    return " ".join(tokens[: i + 1])
            return head
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

    # "Totalt belopp SEK 1 625,00" is the authoritative total; fall back to the
    # summary box "Totalt belopp 1 625,00" at the top of the invoice.
    amount_raw = first_match(lines, rf"Totalt\s+belopp\s+SEK\s+({AMOUNT_RE})")
    if amount_raw is None:
        amount_raw = first_match(lines, rf"Totalt\s+belopp\s+({AMOUNT_RE})")

    # Invoice number and date sit in the "Fakturanummer / Datum / ..." table row.
    invoice_no = None
    invoice_date = None
    for idx, line in enumerate(lines):
        if re.search(r"Fakturanummer\s+Datum", line) and idx + 1 < len(lines):
            row = re.match(rf"^(\S+)\s+({DATE_RE})", lines[idx + 1])
            if row:
                invoice_no, invoice_date = row.group(1), row.group(2)
            break

    giro = first_match(lines, r"Bankgiro\s+(\d{3,4}-\d{4})")
    giro_type = "BG"
    if giro is None:
        plusgiro = first_match(lines, r"Plusgiro\s+([\d\s-]{5,})")
        if plusgiro:
            giro, giro_type = plusgiro.strip(), "PG"

    fields = {
        "payee_name": payee_name(lines),
        "org_number": first_match(lines, r"Organisationnummer:?\s*(\d{6}-?\d{4})"),
        "ocr_number": first_match(lines, r"OCR-nummer\s+(\d{6,25})"),
        "invoice_date": invoice_date,
        "due_date": first_match(lines, rf"F\u00f6rfallodatum\s+({DATE_RE})"),
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
