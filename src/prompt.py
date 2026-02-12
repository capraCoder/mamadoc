"""Claude Vision prompt templates, response parsing, and extraction validation."""

import json
import re

SYSTEM_PROMPT = """\
You are a document analysis assistant for German elder care (Altenpflege) documents.
The user manages care for an elderly parent in Germany.

You will receive a scanned image of a German document. Return ONLY valid JSON — no markdown fences, no explanation.

All summaries, recommendations, and action items must be in English.
Dates: YYYY-MM-DD format.
Amounts: numeric, no currency symbol (EUR implied).

Required JSON structure:
{
  "doc_type": "one of: pflegeheim_invoice, tax_notice, tax_return, health_insurance, care_insurance, medical_report, government_notice, pension, bank_statement, utility_bill, legal_notice, correspondence, pharmacy, other",
  "doc_date": "YYYY-MM-DD or null",
  "sender": "Organization or person who sent this",
  "subject": "Brief subject line in English",
  "reference_numbers": ["any case/invoice/account numbers found"],
  "amount": 1234.56,
  "amounts_detail": [
    {"label": "Description", "amount": 123.45}
  ],
  "deadline": "YYYY-MM-DD or null",
  "urgency": "critical/high/normal/low",
  "summary_en": "Clear English summary (100-200 words): what is this, why sent, what action needed, consequences if ignored.",
  "recommendation_en": "Specific actionable recommendation in English.",
  "action_items": [
    {"action": "Specific action in English", "deadline": "YYYY-MM-DD or null"}
  ],
  "full_text_de": "Complete German text transcription from the document.",
  "key_terms_de": ["important German terms found"],
  "letter_type": "original/reminder/final_notice/receipt/confirmation/information/other"
}

Urgency rules:
- critical: deadline within 7 days OR legal/financial consequence mentioned
- high: deadline within 30 days
- normal: no urgent deadline but action needed
- low: informational only, no action required

letter_type helps with timeline grouping:
- original: first letter about a matter
- reminder: Mahnung, Erinnerung, follow-up
- final_notice: letzte Mahnung, Androhung, legal threat
- receipt: Quittung, Zahlungsbestätigung
- confirmation: Bestätigung, Zusage
- information: pure info, no action needed
"""

USER_PROMPT = """\
Analyze this scanned German document. Extract all information per the JSON structure.
If a field cannot be determined, use null.
For amounts_detail, list every line item you can read.
For full_text_de, transcribe all readable German text.
Be precise with dates, amounts, and reference numbers.\
"""

ISSUE_LINKING_PROMPT = """\
You are matching a newly processed document to existing issues (groups of related documents about the same matter).

New document:
- Sender: {sender}
- Subject: {subject}
- Date: {doc_date}
- Type: {doc_type}
- Reference numbers: {ref_numbers}
- Letter type: {letter_type}

Existing issues:
{issues_list}

Does this document belong to an existing issue? Consider:
- Same sender + same/similar reference number = strong match
- Same sender + same topic/subject + overlapping time period = likely match
- Different sender but same reference number = possible match (e.g., insurance reply to original invoice)

Return ONLY valid JSON:
{{"issue_id": <int or null>, "confidence": <0.0-1.0>, "reason": "brief explanation"}}

Return issue_id=null if this is a new matter.\
"""

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------
VALID_DOC_TYPES = {
    "pflegeheim_invoice", "tax_notice", "tax_return", "health_insurance",
    "care_insurance", "medical_report", "government_notice", "pension",
    "bank_statement", "utility_bill", "legal_notice", "correspondence",
    "pharmacy", "other",
}
VALID_URGENCIES = {"critical", "high", "normal", "low"}
VALID_LETTER_TYPES = {
    "original", "reminder", "final_notice", "receipt",
    "confirmation", "information", "other",
}
REQUIRED_FIELDS = ["doc_type", "sender", "summary_en"]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------
def _find_json_object(text: str) -> str | None:
    """Find the first balanced {...} block in text (brace-depth counting)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_response(text: str) -> dict:
    """Extract JSON from Claude's response, with fallback for markdown fences."""
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text)
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError:
        pass

    # Find first balanced {...} block
    block = _find_json_object(text)
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    return {"_parse_error": True, "_raw_response": text}


def parse_linking_response(text: str) -> dict:
    """Parse issue linking response."""
    result = parse_response(text)
    if "_parse_error" in result:
        return {"issue_id": None, "confidence": 0.0, "reason": "parse error"}
    return result


# ---------------------------------------------------------------------------
# Extraction validation
# ---------------------------------------------------------------------------
def validate_extraction(data: dict) -> dict:
    """Validate and coerce Claude's extraction output.

    Returns the cleaned dict with a ``_warnings`` list (empty if all OK).
    Does NOT raise — always returns usable data.
    """
    if "_parse_error" in data:
        return data  # nothing to validate

    warnings: list[str] = []

    # Required fields
    for field in REQUIRED_FIELDS:
        if not data.get(field):
            warnings.append(f"missing required field: {field}")

    # Enum: doc_type
    if data.get("doc_type") and data["doc_type"] not in VALID_DOC_TYPES:
        warnings.append(f"unknown doc_type '{data['doc_type']}', defaulting to 'other'")
        data["doc_type"] = "other"

    # Enum: urgency
    urg = data.get("urgency")
    if urg and urg not in VALID_URGENCIES:
        warnings.append(f"unknown urgency '{urg}', defaulting to 'normal'")
        data["urgency"] = "normal"

    # Enum: letter_type
    lt = data.get("letter_type")
    if lt and lt not in VALID_LETTER_TYPES:
        warnings.append(f"unknown letter_type '{lt}', defaulting to 'other'")
        data["letter_type"] = "other"

    # Coerce amount to float
    if data.get("amount") is not None:
        try:
            data["amount"] = float(data["amount"])
        except (ValueError, TypeError):
            warnings.append(f"non-numeric amount '{data['amount']}', setting to None")
            data["amount"] = None

    # Validate date formats (YYYY-MM-DD)
    for date_field in ("doc_date", "deadline"):
        val = data.get(date_field)
        if val is not None and not _DATE_RE.match(str(val)):
            warnings.append(f"invalid date format for {date_field}: '{val}'")
            data[date_field] = None

    # Ensure action_items is a list
    if not isinstance(data.get("action_items"), list):
        data["action_items"] = []

    # Ensure reference_numbers is a list
    if not isinstance(data.get("reference_numbers"), list):
        data["reference_numbers"] = []

    # --- Keyword-based letter_type fallback (#13) ---
    if not data.get("letter_type") or data["letter_type"] == "other":
        text_de = (data.get("full_text_de") or "").lower()
        if any(
            kw in text_de
            for kw in ("letzte mahnung", "androhung", "zwangsvollstreckung")
        ):
            data["letter_type"] = "final_notice"
        elif any(
            kw in text_de
            for kw in ("mahnung", "erinnerung", "zahlungserinnerung")
        ):
            data["letter_type"] = "reminder"
        elif any(kw in text_de for kw in ("quittung", "zahlungsbestätigung")):
            data["letter_type"] = "receipt"
        elif any(
            kw in text_de for kw in ("bestätigung", "bescheinigung", "zusage")
        ):
            data["letter_type"] = "confirmation"

    data["_warnings"] = warnings
    return data
