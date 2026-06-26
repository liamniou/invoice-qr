# invoice-qr

Gradio web UI that generates a Swedish payment QR code (Svenska
Bankföreningen unstructured invoice format) you can scan in a Swedish bank app
to pre-fill a bankgiro/plusgiro payment.

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
