"""Swedish invoice QR-code generator (Gradio web UI).

Produces the Swedish Bankers' Association ("Svenska Bankföreningen")
unstructured invoice QR payload that Swedish bank apps scan to pre-fill a
bankgiro/plusgiro payment.
"""

import json
import os
import re

import gradio as gr
import qrcode


def _digits(value: str) -> str:
    """Keep digits only (used for org-number normalisation)."""
    return re.sub(r"\D", "", value or "")


def _normalise_date(value: str) -> str:
    """Accept YYYY-MM-DD or YYYYMMDD and return YYYYMMDD."""
    digits = _digits(value)
    if len(digits) != 8:
        raise ValueError(f"Date must be YYYYMMDD or YYYY-MM-DD, got: {value!r}")
    return digits


def _normalise_amount(value: str) -> float:
    """Accept '1 625,00', '1625.00', '1625' etc. and return a float."""
    cleaned = (value or "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        raise ValueError("Amount is required")
    return round(float(cleaned), 2)


def build_payload(nme, cid, iref, idt, ddt, due, pt, acc):
    return {
        "uqr": 1,              # Unstructured QR-code version
        "tp": 1,               # 1 = invoice
        "nme": nme.strip(),    # Payee name
        "cid": _digits(cid),   # Payee org. number (digits only)
        "iref": iref.strip(),  # Invoice reference / OCR number
        "idt": _normalise_date(idt),  # Invoice date
        "ddt": _normalise_date(ddt),  # Due date
        "due": _normalise_amount(due),  # Amount due (SEK)
        "pt": pt,              # Payment type: BG = bankgiro, PG = plusgiro
        "acc": acc.strip(),    # Bankgiro/plusgiro number
    }


def generate(nme, cid, iref, idt, ddt, due, pt, acc):
    try:
        payload = build_payload(nme, cid, iref, idt, ddt, due, pt, acc)
    except ValueError as exc:
        raise gr.Error(str(exc))

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img, data


with gr.Blocks(title="Swedish Invoice QR") as demo:
    gr.Markdown(
        "# Swedish Invoice QR\n"
        "Generate a payment QR code (Svenska Bankföreningen format) that you "
        "can scan in your Swedish bank app to pre-fill a bankgiro/plusgiro "
        "payment."
    )
    with gr.Row():
        with gr.Column():
            nme = gr.Textbox(label="Payee name", placeholder="BrickHouse Systemkameran AB")
            cid = gr.Textbox(label="Payee org. number", placeholder="559177-1877")
            iref = gr.Textbox(label="Invoice reference / OCR", placeholder="4160000001303")
            with gr.Row():
                idt = gr.Textbox(label="Invoice date", placeholder="2026-06-16")
                ddt = gr.Textbox(label="Due date", placeholder="2026-06-30")
            with gr.Row():
                due = gr.Textbox(label="Amount (SEK)", placeholder="14 875,00")
                pt = gr.Dropdown(label="Payment type", choices=["BG", "PG"], value="BG")
            acc = gr.Textbox(label="Bankgiro / Plusgiro", placeholder="702-2643")
            btn = gr.Button("Generate QR", variant="primary")
        with gr.Column():
            out_img = gr.Image(label="QR code", type="pil")
            out_txt = gr.Textbox(label="QR payload", lines=4)

    btn.click(
        generate,
        inputs=[nme, cid, iref, idt, ddt, due, pt, acc],
        outputs=[out_img, out_txt],
    )


if __name__ == "__main__":
    demo.launch(
        server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
    )
