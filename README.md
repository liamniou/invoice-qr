# invoice-qr

Gradio web UI for Swedish invoices: upload the invoice PDF and it extracts the
payment details and renders a QR code (Svenska Bankföreningen unstructured
invoice format) you can scan in a Swedish bank app to pre-fill a
bankgiro/plusgiro payment.

Fields read from the PDF: payee name, organisation number, OCR reference,
invoice date, due date, total amount and bankgiro/plusgiro number. Every field
stays editable in the UI in case a layout is not recognised, and the OCR
reference is checked against its Luhn check digit.

The PDF must be text-based; scanned images are not OCR'd.

## Run locally

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:7860
```

## Docker

```bash
docker build -t invoice-qr .
docker run --rm -p 7860:7860 invoice-qr
```

The image is published to `ghcr.io/liamniou/invoice-qr` by GitHub Actions on
every push to `main`.
