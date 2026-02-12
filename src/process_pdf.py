"""Core pipeline: PDF -> image -> Claude Vision -> JSON -> DB + issue linking."""

import base64
import io
import json
import sys
from pathlib import Path

import anthropic
from pdf2image import convert_from_path

from . import db
from .config import (
    ANTHROPIC_API_KEY,
    API_MAX_RETRIES,
    API_TIMEOUT,
    CUSTODY_DIR,
    DPI,
    JPEG_QUALITY,
    MAX_PAGES,
    MODEL,
    MODEL_LINKING,
    PROCESSED_DIR,
    check_setup,
    setup_logging,
)
from .prompt import (
    ISSUE_LINKING_PROMPT,
    SYSTEM_PROMPT,
    USER_PROMPT,
    parse_linking_response,
    parse_response,
    validate_extraction,
)

log = setup_logging()


def pdf_to_images(pdf_path: Path, dpi: int = DPI) -> list[bytes]:
    """Convert PDF pages to JPEG bytes."""
    images = convert_from_path(str(pdf_path), dpi=dpi)
    result = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        result.append(buf.getvalue())
    return result


def analyze_page(client: anthropic.Anthropic, image_bytes: bytes) -> dict:
    """Send one page image to Claude Vision, get structured extraction."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": USER_PROMPT,
                    },
                ],
            }
        ],
    )
    return parse_response(message.content[0].text)


def merge_extractions(extractions: list[dict]) -> dict:
    """Merge multi-page extraction results into one document."""
    if len(extractions) == 1:
        return extractions[0]

    # Start with page 1 metadata
    merged = dict(extractions[0])

    # Concatenate text fields
    all_text = []
    all_actions = []
    all_amounts = []
    all_refs = []
    all_terms = []

    for ext in extractions:
        if ext.get("full_text_de"):
            all_text.append(ext["full_text_de"])
        all_actions.extend(ext.get("action_items") or [])
        all_amounts.extend(ext.get("amounts_detail") or [])
        all_refs.extend(ext.get("reference_numbers") or [])
        all_terms.extend(ext.get("key_terms_de") or [])

    merged["full_text_de"] = "\n\n--- PAGE BREAK ---\n\n".join(all_text)
    merged["action_items"] = all_actions
    merged["amounts_detail"] = all_amounts
    merged["reference_numbers"] = list(dict.fromkeys(all_refs))  # dedupe, preserve order
    merged["key_terms_de"] = list(dict.fromkeys(all_terms))

    # Use highest urgency across pages
    urgency_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    best_urgency = min(
        (ext.get("urgency", "normal") for ext in extractions),
        key=lambda u: urgency_rank.get(u, 9),
    )
    merged["urgency"] = best_urgency

    # Sum amounts if page 1 didn't capture total
    if not merged.get("amount") and all_amounts:
        merged["amount"] = sum(a.get("amount", 0) for a in all_amounts)

    return merged


def link_to_issue(client: anthropic.Anthropic, extraction: dict, doc_id: int):
    """Try to link a document to an existing issue, or create a new one."""
    existing = db.get_issues_summary_for_linking()

    if existing:
        # --- Rule-based fast-match: same sender + same ref_number (#11) ---
        ref_nums = set(extraction.get("reference_numbers") or [])
        sender_lower = (extraction.get("sender") or "").lower().strip()
        for iss in existing:
            iss_sender = (iss.get("sender") or "").lower().strip()
            iss_ref = iss.get("ref_number") or ""
            if sender_lower and sender_lower == iss_sender and iss_ref and iss_ref in ref_nums:
                db.link_document_to_issue(doc_id, iss["id"])
                log.info(f"  Rule-matched to issue #{iss['id']} (sender+ref)")
                return

        # --- Claude-assisted linking ---
        issues_text = "\n".join(
            f"- Issue #{iss['id']}: {iss['title']} | sender: {iss['sender']} | "
            f"ref: {iss['ref_number']} | category: {iss['category']} | "
            f"dates: {iss['first_seen']} to {iss['latest_date']} | "
            f"{iss['doc_count']} docs | status: {iss['status']}"
            for iss in existing
        )

        prompt_text = ISSUE_LINKING_PROMPT.format(
            sender=extraction.get("sender", "unknown"),
            subject=extraction.get("subject", "unknown"),
            doc_date=extraction.get("doc_date", "unknown"),
            doc_type=extraction.get("doc_type", "unknown"),
            ref_numbers=", ".join(extraction.get("reference_numbers") or ["none"]),
            letter_type=extraction.get("letter_type", "unknown"),
            issues_list=issues_text,
        )

        try:
            message = client.messages.create(
                model=MODEL_LINKING,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt_text}],
            )
            result = parse_linking_response(message.content[0].text)

            if result.get("issue_id") and result.get("confidence", 0) >= 0.6:
                db.link_document_to_issue(doc_id, result["issue_id"])
                log.info(f"  Linked to issue #{result['issue_id']}: {result.get('reason', '')}")
                return
        except Exception as e:
            log.error(f"  Issue linking API error: {e}")

    # Create new issue
    ref_nums_list = extraction.get("reference_numbers") or []
    issue_id = db.create_issue(
        {
            "title": f"{extraction.get('sender', 'Unknown')} — {extraction.get('subject', 'Document')}",
            "sender": extraction.get("sender"),
            "ref_number": ref_nums_list[0] if ref_nums_list else None,
            "category": extraction.get("doc_type"),
            "first_seen": extraction.get("doc_date"),
            "latest_date": extraction.get("doc_date"),
            "latest_deadline": extraction.get("deadline"),
            "urgency": extraction.get("urgency", "normal"),
        }
    )
    db.link_document_to_issue(doc_id, issue_id)
    log.info(f"  Created new issue #{issue_id}")


def process_pdf(pdf_path: Path, force: bool = False) -> dict | None:
    """Full pipeline: PDF -> images -> Claude -> JSON -> DB.

    Args:
        pdf_path: Path to the PDF file.
        force: If True, reprocess even if already in DB.

    Returns:
        Extraction dict on success, None on skip/error.
    """
    stem = pdf_path.stem

    if not force and db.is_processed(pdf_path.name):
        log.info(f"Already processed: {pdf_path.name}")
        return db.get_document_by_filename(pdf_path.name)

    log.info(f"Processing: {pdf_path.name}")

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=API_TIMEOUT,
        max_retries=API_MAX_RETRIES,
    )

    # Convert PDF to images
    log.info("  Converting PDF to images...")
    pages = pdf_to_images(pdf_path)

    # Page count guard (#14)
    if len(pages) > MAX_PAGES:
        log.warning(f"  Skipping {pdf_path.name}: {len(pages)} pages exceeds limit {MAX_PAGES}")
        return None

    log.info(f"  {len(pages)} page(s), sizes: {[len(p) for p in pages]} bytes")

    # Track created files for cleanup on failure (#5)
    created_files: list[Path] = []
    try:
        # Save page images
        PROCESSED_DIR.mkdir(exist_ok=True)
        for i, img_bytes in enumerate(pages):
            img_path = PROCESSED_DIR / f"{stem}_p{i + 1}.jpg"
            img_path.write_bytes(img_bytes)
            created_files.append(img_path)

        # Analyze each page with Claude Vision
        extractions = []
        for i, img_bytes in enumerate(pages):
            log.info(f"  Analyzing page {i + 1}/{len(pages)}...")
            result = analyze_page(client, img_bytes)
            if "_parse_error" in result:
                log.warning(f"  Parse error on page {i + 1}")
            extractions.append(result)

        # Merge multi-page results
        merged = merge_extractions(extractions)
        merged["page_count"] = len(pages)

        # Validate extraction (#3)
        merged = validate_extraction(merged)
        if merged.get("_warnings"):
            for w in merged["_warnings"]:
                log.warning(f"  Validation: {w}")

        # Save full JSON
        json_path = PROCESSED_DIR / f"{stem}.json"
        json_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        created_files.append(json_path)

        # Atomic DB upsert (#2)
        doc_id = db.upsert_document_with_actions(
            {
                "filename": pdf_path.name,
                "doc_type": merged.get("doc_type"),
                "doc_date": merged.get("doc_date"),
                "sender": merged.get("sender"),
                "subject": merged.get("subject"),
                "amount": merged.get("amount"),
                "deadline": merged.get("deadline"),
                "urgency": merged.get("urgency", "normal"),
                "letter_type": merged.get("letter_type"),
                "summary_en": merged.get("summary_en"),
                "recommendation": merged.get("recommendation_en"),
                "json_path": str(json_path),
                "page_count": len(pages),
            },
            merged.get("action_items") or [],
        )

        # Link to issue
        log.info("  Linking to issue...")
        link_to_issue(client, merged, doc_id)

        log.info(f"  Done: {merged.get('doc_type')} | {merged.get('subject')}")
        return merged

    except Exception as e:
        log.error(f"  FAILED: {pdf_path.name}: {e}")
        # Cleanup created files on failure (#5)
        for f in created_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def process_all(force: bool = False) -> tuple[list[dict], list[str]]:
    """Process all unprocessed PDFs in the custody directory.

    Returns:
        (results, failed_filenames)
    """
    pdf_files = sorted(CUSTODY_DIR.glob("*.pdf"))
    if force:
        unprocessed = pdf_files
    else:
        unprocessed = [f for f in pdf_files if not db.is_processed(f.name)]

    if not unprocessed:
        log.info("No new PDFs to process.")
        return [], []

    total = len(unprocessed)
    log.info(f"Processing {total} PDF(s)...")
    results = []
    failed = []
    for idx, pdf_path in enumerate(unprocessed, 1):
        log.info(f"[{idx}/{total}] {pdf_path.name}")
        try:
            result = process_pdf(pdf_path, force=force)
            if result:
                results.append(result)
        except Exception as e:
            log.error(f"  ERROR: {e}")
            failed.append(pdf_path.name)

    log.info(f"Batch complete: {len(results)} OK, {len(failed)} failed")
    if failed:
        log.info(f"Failed: {', '.join(failed)}")
    return results, failed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Process custody PDFs")
    parser.add_argument("pdf", nargs="?", help="Single PDF to process")
    parser.add_argument("--force", action="store_true", help="Reprocess even if already done")
    args = parser.parse_args()

    if not check_setup():
        sys.exit(1)

    db.init_db()

    if args.pdf:
        process_pdf(Path(args.pdf), force=args.force)
    else:
        process_all(force=args.force)
