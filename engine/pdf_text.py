#!/usr/bin/env python3
"""
pdf_text.py — extract text from a PDF attachment (Blueprint M18, attachment reading).

Vendor/insurer decisions routinely arrive as a PDF (the Stride and the AmEx emails both did),
so the classifier must read attachments, not just the email body. Uses PyMuPDF (`fitz`), which is
already in the container venv — no new dependency.

Usage in the pipeline: the inbox watcher fetches a matched email's PDF attachment bytes (via the
VPS Gmail token, the same one inbox_watcher.py already uses), calls extract_text(), and appends the
result to the classifier input so a decision hidden in a PDF is understood.
"""
import fitz  # PyMuPDF


def extract_text(pdf_bytes):
    """Return the text of a PDF given its raw bytes. Empty string if it can't be read."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""
    parts = []
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n".join(parts).strip()


if __name__ == "__main__":
    # self-test: synthesize a PDF that mimics a denial decision, read it back
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72),
                     "AMEX Assurance Company\nClaim 12393058\nOutcome: your refund request was "
                     "DENIED - the claim was filed out of time.")
    data = doc.tobytes()
    doc.close()
    txt = extract_text(data)
    print("extracted:", repr(txt[:140]))
    assert "DENIED" in txt and "12393058" in txt, "PDF EXTRACTION SELF-TEST FAILED"
    print("PASS — fitz reads PDF attachments; a decision buried in a PDF is now visible to the classifier.")
