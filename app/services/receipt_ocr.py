"""OCR-based extraction of payee / amount / date from a receipt photo.

Uses Tesseract OCR via `pytesseract` — free, runs entirely on this machine
(no cloud upload, no API key). Requires:
  1. The Python packages `pytesseract` and `Pillow` (see requirements.txt).
  2. The Tesseract OCR *program* itself installed separately:
       macOS:   brew install tesseract
       Ubuntu:  sudo apt-get install tesseract-ocr

Accuracy note: this is a best-effort heuristic, not a guarantee. Clean,
well-lit, printed (non-handwritten) receipts work best. The extracted
fields are meant to be reviewed/corrected by the user before submitting —
never trust them blindly.
"""
import io
import re
from datetime import datetime


def ocr_image_bytes(image_bytes: bytes) -> str:
    """Run OCR on raw image bytes and return the recognized text."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "OCR support isn't installed. Run: pip install pytesseract Pillow"
        ) from exc

    image = Image.open(io.BytesIO(image_bytes))
    try:
        return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract OCR isn't installed on this machine. "
            "On macOS, run: brew install tesseract"
        ) from exc


def extract_receipt_fields(text: str) -> dict:
    """Best-effort extraction of payee name, date, and total amount from
    raw OCR text of a receipt. Returns {"payee_name", "due_date", "amount"}
    — any field that can't be confidently found is left as an empty string.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # ── Payee / merchant name: first non-empty line that isn't a date ──
    payee_name = ""
    for line in lines:
        if re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", line):
            continue
        if len(line) >= 3:
            payee_name = line
            break

    # ── Date ──
    due_date = ""
    date_patterns = [
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",       # YYYY-MM-DD
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b",  # MM/DD/YYYY
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2})\b",  # MM/DD/YY
    ]
    for pat in date_patterns:
        m = re.search(pat, text)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups[0]) == 4:  # YYYY-MM-DD
                y, mo, d = groups
            else:
                mo, d, y = groups
                if len(y) == 2:
                    y = "20" + y
            candidate = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            datetime.strptime(candidate, "%Y-%m-%d")  # sanity check
        except (ValueError, IndexError):
            continue
        due_date = candidate
        break

    # ── Amount: prefer explicit TOTAL / GRAND TOTAL / AMOUNT DUE lines ──
    amount = ""
    number_pat = r"([0-9]{1,3}(?:,[0-9]{3})+\s*\.\s*[0-9]{1,2}|[0-9]+(?:\s*\.\s*[0-9]{1,2})?)"
    best = None
    for line in lines:
        low = line.lower()
        if "subtotal" in low or "sub-total" in low:
            continue
        if re.search(r"\btotal\b", low) or "amount due" in low:
            m = re.search(number_pat, line)
            if m:
                candidate = m.group(1)
                priority = 2 if ("grand" in low or "due" in low) else 1
                if best is None or priority >= best[1]:
                    best = (candidate, priority)
    if best:
        amount = best[0].replace(",", "").replace(" ", "")

    # 保底规则: 如果没有抓到 total 关键字(常见于 OCR 把 "Total" 认错,或收据
    # 根本没印这个字),改成抓全文里所有「两位小数」格式的金额,取最大的一个。
    if not amount:
        decimal_pat = r"([0-9]{1,3}(?:,[0-9]{3})+\s*\.\s*[0-9]{2}|[0-9]+\s*\.\s*[0-9]{2})\b"
        decimal_candidates = []
        for line in lines:
            for m in re.finditer(decimal_pat, line):
                try:
                    val = float(m.group(1).replace(",", "").replace(" ", ""))
                except ValueError:
                    continue
                if val > 0:
                    decimal_candidates.append(val)
        if decimal_candidates:
            amount = f"{max(decimal_candidates):.2f}"

    line_items = _extract_line_items(lines, amount)
    for item in line_items:
        item["account_code"] = guess_account_code(item["description"])
    account_code = guess_account_code(payee_name)

    return {
        "payee_name": payee_name,
        "due_date": due_date,
        "amount": amount,
        "line_items": line_items,
        "account_code": account_code,
    }


_SUMMARY_KEYWORDS = ("subtotal", "sub-total", "total", "vat", "tax", "discount",
                     "service charge", "amount due", "amount payable", "please pay",
                     "amount to pay", "change", "cash", "balance")
_SKIP_KEYWORDS = ("receipt no", "invoice no", "or no", "tin", "date", "website",
                  "www.", ".com", "tel", "phone", "email", "cashier", "welcome",
                  "thank you", "come again", "please", "signature", "card ending",
                  "approved", "authorization", "terminal", "reference", "time:",
                  "table", "server", "queue", "member", "points", "loyalty")
_DECIMAL_PAT = r"([0-9]{1,3}(?:,[0-9]{3})+\s*\.\s*[0-9]{2}|[0-9]+\s*\.\s*[0-9]{2})\b"
_CHARGE_KEYWORDS = ("vat", "tax", "service charge")


def _is_summary_line(line: str) -> bool:
    low = line.lower()
    return any(k in low for k in _SUMMARY_KEYWORDS)


def _is_skippable(line: str, idx: int) -> bool:
    low = line.lower()
    if idx == 0:
        return True
    if any(k in low for k in _SKIP_KEYWORDS):
        return True
    if re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", line):
        return True
    if "," in line and re.search(r"\d", line):
        return True
    if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\s*\.\s*[0-9]{1,2})?|[0-9]+(?:\s*\.\s*[0-9]{1,2})?", line.strip()):
        return True
    return False


def _clean_label(label: str) -> str:
    return re.sub(_DECIMAL_PAT + r"\s*$", "", label).strip(" -:\t")


def _extract_line_items(lines: list, grand_total: str) -> list:
    """Best-effort split of a receipt into per-item description/amount pairs,
    including a real VAT/tax/service-charge line when the receipt has one (so
    the line items still add up to the true reimbursable total, not just the
    pre-tax subtotal). Only returns results when the item count and number
    count line up exactly AND the amounts reconcile to the receipt's own
    grand total — otherwise it's too risky to guess, so it returns an empty
    list (or just the product lines) and the caller falls back safely."""
    summary_start = None
    for i, line in enumerate(lines):
        if _is_summary_line(line):
            summary_start = i
            break
    if summary_start is None:
        return []

    candidate_lines = [
        line for i, line in enumerate(lines[:summary_start])
        if not _is_skippable(line, i)
    ]
    if not candidate_lines:
        return []
    # If any candidate line already carries its own inline amount (e.g. "Base
    # Fare 120.00"), this is an inline-format receipt — restrict item_labels
    # to just those lines, since plain header/metadata text before the summary
    # block (e.g. "Trip ID: GR-88213", "Pick-up: Ayala Ave") never has its own
    # amount and would otherwise get miscounted as an item. If nothing has an
    # inline amount, this is a two-column receipt (labels and numbers OCR'd as
    # separate blocks) and every candidate line is a real item label.
    inline_items = [line for line in candidate_lines if re.search(_DECIMAL_PAT, line)]
    item_labels = inline_items if inline_items else candidate_lines

    summary_lines = []
    for line in lines[summary_start:]:
        if _is_summary_line(line):
            summary_lines.append(line)
        else:
            break
    summary_count = len(summary_lines)

    all_numbers = []
    for line in lines:
        for m in re.finditer(_DECIMAL_PAT, line):
            all_numbers.append(m.group(1).replace(",", "").replace(" ", ""))

    expected_count = len(item_labels) + summary_count
    if len(all_numbers) != expected_count:
        return []

    item_amounts = all_numbers[:len(item_labels)]
    summary_amounts = all_numbers[len(item_labels):len(item_labels) + summary_count]

    try:
        items_sum = sum(float(a) for a in item_amounts)
        total_val = float(grand_total) if grand_total else None
    except (TypeError, ValueError):
        return []
    if total_val is None:
        return []

    # Pull out real additional charges (VAT/tax/service charge) so the
    # reimbursement isn't silently short by the tax amount. Subtotal/total/
    # discount/cash/change/balance lines are deliberately not itemized here.
    charge_items = []
    charge_total = 0.0
    for label, amt in zip(summary_lines, summary_amounts):
        low = label.lower()
        if not any(k in low for k in _CHARGE_KEYWORDS):
            continue
        try:
            val = float(amt)
        except ValueError:
            continue
        charge_items.append({"description": _clean_label(label), "amount": f"{val:.2f}"})
        charge_total += val

    combined_sum = items_sum + charge_total
    if abs(combined_sum - total_val) <= 0.05:
        results = [{"description": _clean_label(l) or l, "amount": a} for l, a in zip(item_labels, item_amounts)]
        results.extend(charge_items)
        return results

    if items_sum <= total_val + 0.05:
        return [{"description": _clean_label(l) or l, "amount": a} for l, a in zip(item_labels, item_amounts)]

    return []


# Best-effort merchant/item keyword -> chart-of-accounts code. Checked in order;
# first match wins. Codes must match real entries in app/db.py's SEED_ACCOUNTS.
# Unknown merchants simply return None — we never guess a fallback/suspense code.
_ACCOUNT_KEYWORDS = [
    ("6609", ["parking", "toll", "easytrip", "autosweep"]),
    ("6608", ["gasoline", "gas station", "shell", "petron", "caltex", "unioil", "seaoil"]),
    ("6614", ["office supplies", "national bookstore", "office warehouse", "officewarehouse",
              "staples", "bond paper", "ballpen", "stapler", "notebook", "ink cartridge", "toner"]),
    ("6604", ["globe", "smart communications", "pldt", "converge", "sky cable", "telecom", "internet"]),
    ("6618", ["seminar", "training", "webinar", "conference"]),
    ("6625", ["subscription", "software", "saas", "adobe", "microsoft 365", "zoom",
              "google workspace", "aws", "canva"]),
    ("6610001", ["grab", "angkas", "joyride", "taxi", "uber", "lalamove", "airline",
                 "airlines", "hotel", "booking.com", "agoda"]),
    ("6613", ["starbucks", "jollibee", "mcdonald", "kfc", "restaurant", "cafe", "coffee",
              "food court", "chowking", "greenwich"]),
]


def guess_account_code(text: str):
    """Best-effort GL account guess from merchant/item text. Returns None when
    nothing matches — callers should leave the account unset in that case
    rather than defaulting to a possibly-wrong code."""
    if not text:
        return None
    low = text.lower()
    for code, keywords in _ACCOUNT_KEYWORDS:
        if any(kw in low for kw in keywords):
            return code
    return None
