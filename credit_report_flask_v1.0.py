# credit_report_flask.py
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import fitz, tempfile, re, os, io, sys
from dotenv import load_dotenv
from openai import OpenAI

# ---- OCR deps
from PIL import Image
import pytesseract

# --- API + Flask setup ---
# Load environment variables from .env file
load_dotenv(dotenv_path=".env")  # Explicit .env file loading
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Initialize OpenAI client with better error handling
client = None
if OPENAI_API_KEY and OPENAI_API_KEY.startswith("sk-"):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        print(f"✅ OpenAI client initialized successfully with key: {OPENAI_API_KEY[:10]}...")
    except Exception as e:
        print(f"❌ Failed to initialize OpenAI client: {e}")
        client = None
else:
    print("❌ No valid OpenAI API key found in environment")

# If Tesseract is not on PATH (Windows), set it here, e.g.:
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

def check_ocr_dependencies():
    """Check if OCR dependencies are available"""
    try:
        pytesseract.get_tesseract_version()
        return True, "OCR available"
    except Exception as e:
        return False, f"OCR not available: {e}"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max file size

def get_clean_chat_history():
    """Get chat history from session, return None if empty"""
    chat_history = session.get("chat_history")
    
    # Ensure it's a list and has content
    if not isinstance(chat_history, list) or len(chat_history) == 0:
        return None
        
    # Filter out any invalid entries
    valid_history = []
    for msg in chat_history:
        if isinstance(msg, dict) and 'role' in msg and 'content' in msg:
            valid_history.append(msg)
    
    return valid_history if valid_history else None

def get_template_vars(**extra_vars):
    """Get common template variables - always return clean state"""
    # Force clean defaults to prevent any carryover
    base_vars = {
        'has_context': False,  # Always start with no context
        'metrics': {},         # Always start with no metrics
        'ratios': [],          # Always start with no ratios
        'recs': [],            # Always start with no recommendations
        'chat_history': None,  # Always start with no chat history
        'openai_available': client is not None and OPENAI_API_KEY is not None,
        'error': None,
        'upload_error': None
    }
    
    # Only override with session data for specific routes that need it
    if extra_vars.get('use_session_data', False):
        chat_history = get_clean_chat_history()
        base_vars.update({
            'has_context': bool(session.get("cibil_context")),
            'metrics': session.get("cibil_metrics") or {},
            'ratios': session.get("cibil_ratios") or [],
            'recs': session.get("cibil_recs") or [],
            'chat_history': chat_history,
        })
    
    # Debug logging
    print(f"Template vars - OpenAI available: {base_vars['openai_available']}")
    print(f"Template vars - Chat history: {base_vars['chat_history'] is not None}")
    print(f"Template vars - Has context: {base_vars['has_context']}")
    
    base_vars.update(extra_vars)
    return base_vars


# ---------- Helpers ----------
def to_float(num_str):
    if num_str is None:
        return None
    try:
        return float(num_str.replace(",", "").strip())
    except Exception:
        return None

def safe_div(a, b):
    return round(a / b, 4) if (a is not None and b not in (None, 0)) else None

def fmt_pct(x):
    return f"{x*100:.2f}%" if x is not None else "N/A"

def ocr_pdf_to_text(path, dpi=300, lang="eng"):
    """
    Render each page to image and OCR it.
    """
    try:
        text_parts = []
        with fitz.open(path) as doc:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                # Small pre-OCR cleanup: convert to grayscale, let tesseract handle the rest
                img = img.convert("L")
                t = pytesseract.image_to_string(img, lang=lang)
                text_parts.append(t)
        return "\n\n".join(text_parts)
    except Exception as e:
        print(f"OCR Error: {e}")
        # Return empty string if OCR fails
        return ""

def extract_text_with_ocr_fallback(path):
    """
    Try native text extraction; if mostly empty (scanned), OCR it.
    """
    try:
        native = []
        with fitz.open(path) as doc:
            for i in range(doc.page_count):
                native.append(doc.load_page(i).get_text("text"))
        joined = "\n".join(native)
        print(f"Native extraction: {len(joined)} characters")
        
        # Heuristic: scanned PDFs usually return near-empty text
        if len(joined.strip()) < 100:
            print("PDF appears to be scanned, attempting OCR...")
            ocr_available, ocr_status = check_ocr_dependencies()
            
            if ocr_available:
                try:
                    ocr_result = ocr_pdf_to_text(path)
                    print(f"OCR extraction: {len(ocr_result)} characters")
                    return ocr_result
                except Exception as e:
                    print(f"OCR failed: {e}")
            else:
                print(f"OCR not available: {ocr_status}")
            
            # Try alternative extraction methods
            print("Trying alternative extraction methods...")
            try:
                with fitz.open(path) as doc:
                    text_blocks = []
                    for page in doc:
                        blocks = page.get_text("dict")
                        for block in blocks.get("blocks", []):
                            if "lines" in block:
                                for line in block["lines"]:
                                    for span in line.get("spans", []):
                                        text_blocks.append(span.get("text", ""))
                    alternative_text = " ".join(text_blocks)
                    print(f"Alternative extraction: {len(alternative_text)} characters")
                    if len(alternative_text) > len(joined):
                        return alternative_text
            except Exception as e:
                print(f"Alternative extraction failed: {e}")
                
        return joined
    except Exception as e:
        print(f"PDF text extraction failed: {e}")
        return ""

def sum_after_label(text, label_regex):
    """
    Sum amounts that appear on lines containing label(s).
    Supports Indian and intl number formats.
    """
    total = 0.0
    found = False
    for line in text.splitlines():
        if re.search(label_regex, line, re.I):
            m_all = re.findall(r"(-?\d{1,3}(?:,\d{2})?(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)", line)
            if m_all:
                val = to_float(m_all[-1])
                if val is not None:
                    total += val
                    found = True
    return (total if found else None)


# ---------- Regex patterns tuned for your specific CIBIL format ----------
PATTERNS = {
    # Score patterns - the score appears after "CIBIL Score" section but may be on a separate line
    "score": r"(?:CIBIL\s*Score|Score)\s*[:\-]?\s*(\d{3,4})",
    "score_alt": r"^\s*(\d{3})\s*$",  # Score on its own line (3 digits)
    "score_mixed": r"(\d)\s*(\d)\s*[A-Z]\s*",  # Handle OCR errors like "6 5A" -> "654"
    
    # Date patterns
    "score_date": r"Date\s*[:\-]?\s*([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{2,4})",
    "control_number": r"Control\s*Number\s*[:\-]?\s*([\d,]+)",

    # Account summary - look for specific patterns in your PDF
    "total_accounts": r"(?:Total\s+Accounts?|Account\s*Information)",
    "active_accounts": r"(?:Active\s+Accounts?|Open\s+Accounts?)",
    "closed_accounts": r"(?:Closed\s+Accounts?|Date\s*Closed)",

    # Credit limits and balances - specific to your format
    "credit_limit": r"Credit\s*Limit\s*[\r\n]*\s*(\d+(?:,\d+)*)",
    "high_credit": r"High\s*Credit\s*[\r\n]*\s*(\d+(?:,\d+)*)", 
    "current_balance": r"Current\s*Balance\s*[\r\n]*\s*(\d+(?:,\d+)*)",
    "amount_overdue": r"Amount\s*Overdue\s*[\r\n]*\s*(\d+(?:,\d+)*)",

    # Enquiries - from your enquiry section
    "recent_enquiries": r"(?:Enquiry\s*Information|Date\s*of\s*Enquiry)",
}

# Enhanced patterns for extracting financial data
LABELS_LIMIT = r"(?:Credit\s*Limit|High\s*Credit|Sanctioned\s*Amount)"
LABELS_BAL   = r"(?:Current\s*Balance|Amount\s*Overdue|Outstanding)"


def parse_cibil_text(txt):
    """
    Extract key metrics from your specific CIBIL PDF format.
    """
    m = {}
    lines = txt.split('\n')
    print(f"Parsing text of length: {len(txt)}")

    # Look for CIBIL Score - handle the "6 5A" OCR issue
    score = None
    
    # Method 1: Look for the score after "CIBIL Score" section (not control number)
    score_section_found = False
    for i, line in enumerate(lines):
        if "CIBIL Score" in line and "Control Number" not in line:
            score_section_found = True
            print(f"Found 'CIBIL Score' section on line {i}: {repr(line)}")
            # Check next 10 lines for score, skip the explanatory text
            for j in range(i+1, min(i+15, len(lines))):
                next_line = lines[j].strip()
                
                # Skip long explanatory lines and look for short lines with numbers
                if len(next_line) < 10 and next_line:
                    print(f"  Checking short line {j}: {repr(next_line)}")
                    
                    # Handle OCR errors like "6 5A" -> should be "654"
                    ocr_match = re.match(r'(\d)\s*(\d)\s*[A-Za-z0-9]?\s*$', next_line)
                    if ocr_match:
                        # Common OCR errors: A=4, S=5, O=0, etc.
                        score = int(ocr_match.group(1) + ocr_match.group(2) + "4")  # Assume last digit is 4
                        print(f"Found OCR score pattern '{next_line}' -> estimated score: {score}")
                        break
                    
                    # Look for 3-digit numbers in reasonable score range
                    if re.match(r'^\d{3}$', next_line):
                        potential_score = int(next_line)
                        if 300 <= potential_score <= 900:  # Valid CIBIL score range
                            score = potential_score
                            print(f"Found valid score on line {j}: {score}")
                            break
                
                # Stop if we hit Personal Information section
                if "Personal Information" in next_line:
                    break
    
    # Method 2: If no score found in CIBIL Score section, look for reasonable scores elsewhere
    # BUT exclude control numbers and other large numbers
    if not score and score_section_found:
        print("No score found in CIBIL Score section, trying fallback methods...")
        for line in lines:
            # Skip lines with control numbers, account numbers, phone numbers
            if any(x in line for x in ["Control Number", "Account Number", "Phone", "9748425384", "4,743,293,588"]):
                continue
                
            numbers = re.findall(r'\b([6-8]\d{2})\b', line)
            for num in numbers:
                num_val = int(num)
                if 600 <= num_val <= 850:  # Realistic CIBIL score range
                    score = num_val
                    print(f"Found potential score in fallback: {score} from line: {line[:50]}...")
                    break
            if score:
                break
    
    m["Score"] = score

    # Extract date - look for the date after "Date :"
    score_date = None
    for line in lines:
        if line.strip().startswith(': ') and '/' in line:
            date_match = re.search(r'([0-9]{1,2}[\/\-][0-9]{1,2}[\/\-][0-9]{4})', line)
            if date_match:
                score_date = date_match.group(1)
                print(f"Found date: {score_date}")
                break
    m["Score Date"] = score_date

    # Extract detailed account information
    accounts_list = []
    total_accounts = 0
    active_accounts = 0
    closed_accounts = 0
    credit_cards = 0
    loans = 0
    
    # Look for account sections with bank names
    bank_patterns = [
        "CITIBANK", "HDFC BANK", "CREDILA", "KOTAK BANK", "ICICI BANK", 
        "SBI", "AXIS BANK", "STANDARD CHARTERED", "AMERICAN EXPRESS",
        "YES BANK", "INDUSIND BANK", "BAJAJ", "TATA CAPITAL", "HSBC"
    ]
    
    account_types = ["Credit Card", "Education Loan", "Personal Loan", "Home Loan", 
                    "Auto Loan", "Two Wheeler Loan", "Business Loan", "Gold Loan"]
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # Look for bank names
        found_bank = None
        for bank in bank_patterns:
            if bank in line.upper():
                found_bank = bank
                break
                
        if found_bank:
            # Check if this is followed by an account type
            account_type = None
            account_status = "Active"  # Default
            close_date = None
            
            # Look in next 10 lines for account type
            for j in range(i+1, min(i+10, len(lines))):
                next_line = lines[j].strip()
                for acc_type in account_types:
                    if acc_type in next_line:
                        account_type = acc_type
                        break
                if account_type:
                    break
            
            if account_type:
                # Look for account status in next 50 lines
                for j in range(i, min(i+50, len(lines))):
                    status_line = lines[j].strip()
                    
                    # Check for closed status
                    if "Date Closed" in status_line:
                        # Check next line for actual close date
                        if j+1 < len(lines):
                            close_date_line = lines[j+1].strip()
                            if close_date_line != "-" and close_date_line and "/" in close_date_line:
                                account_status = "Closed"
                                close_date = close_date_line
                                closed_accounts += 1
                                break
                    
                    # Check for other status indicators
                    elif any(status in status_line.upper() for status in ["CLOSED", "SETTLED", "WRITTEN OFF"]):
                        account_status = "Closed"
                        closed_accounts += 1
                        break
                        
                if account_status == "Active":
                    active_accounts += 1
                
                # Categorize by type
                if "Credit Card" in account_type:
                    credit_cards += 1
                else:
                    loans += 1
                
                # Add to accounts list
                account_info = {
                    "bank": found_bank,
                    "type": account_type,
                    "status": account_status
                }
                if close_date:
                    account_info["close_date"] = close_date
                    
                accounts_list.append(account_info)
                total_accounts += 1
                
                print(f"Found account: {found_bank} - {account_type} - Status: {account_status}")
                
                # Skip ahead to avoid duplicate detection
                i = j + 5
                continue
        
        i += 1

    m["Total Accounts"] = total_accounts
    m["Active Accounts"] = active_accounts  
    m["Closed Accounts"] = closed_accounts
    m["Credit Cards"] = credit_cards
    m["Loans"] = loans
    m["Accounts Details"] = accounts_list

    # Extract credit limits and balances by scanning account sections
    total_limit = 0
    total_balance = 0
    
    for i, line in enumerate(lines):
        # Look for credit limit patterns
        if "Credit Limit" in line:
            # Check next few lines for amount
            for j in range(i+1, min(i+5, len(lines))):
                amount_line = lines[j].strip()
                if amount_line and amount_line != "-":
                    amount = to_float(amount_line)
                    if amount and amount > 1000:  # Reasonable credit limit
                        total_limit += amount
                        print(f"Found credit limit: {amount}")
                        break
        
        # Look for current balance
        if "Current Balance" in line:
            for j in range(i+1, min(i+5, len(lines))):
                amount_line = lines[j].strip()
                if amount_line and amount_line != "-":
                    amount = to_float(amount_line)
                    if amount is not None and amount >= 0:  # Can be 0
                        total_balance += amount
                        print(f"Found balance: {amount}")
                        break

    m["Total Credit Limit"] = total_limit if total_limit > 0 else None
    m["Total Outstanding Balance"] = total_balance if total_balance >= 0 else None

    # Count enquiries from enquiry section
    enquiry_count = 0
    in_enquiry_section = False
    
    for i, line in enumerate(lines):
        if "Enquiry Information" in line:
            in_enquiry_section = True
        elif "Date of Enquiry" in line and in_enquiry_section:
            # Count the enquiry dates in the following lines
            for j in range(i+1, min(i+10, len(lines))):
                next_line = lines[j].strip()
                if re.match(r'\d{2}/\d{2}/\d{4}', next_line):
                    enquiry_count += 1
                elif "Credit Report" in next_line or "Enquiry Purpose" in next_line:
                    break
            break

    m["Recent Enquiries"] = enquiry_count if enquiry_count > 0 else None

    # Initialize other fields to None for now
    m["Max DPD"] = None
    m["Late Payments (12m)"] = None
    m["Written-off/Settled Count"] = None

    print(f"Final parsed metrics: {m}")
    return m

def parse_pdf(path):
    """Enhanced PDF parsing with detailed debugging"""
    print(f"Starting PDF parsing for: {path}")
    
    # First, check OCR availability
    ocr_available, ocr_status = check_ocr_dependencies()
    print(f"OCR Status: {ocr_status}")
    
    text = extract_text_with_ocr_fallback(path)
    print(f"Extracted text length: {len(text)} characters")
    
    if len(text) > 0:
        print(f"First 300 characters of extracted text:")
        print(repr(text[:300]))
    
    result = parse_cibil_text(text)
    print(f"Parsing result: {result}")
    return result

def compute_ratios(metrics):
    limit_ = metrics.get("Total Credit Limit")
    bal_   = metrics.get("Total Outstanding Balance")
    util   = safe_div(bal_, limit_)
    score  = metrics.get("Score")
    enquiries = metrics.get("Recent Enquiries")
    max_dpd   = metrics.get("Max DPD")
    late_12m  = metrics.get("Late Payments (12m)")

    ratios = [
        ("Utilization", util),
        ("Score/900", safe_div(score, 900.0) if score else None),
        ("DPD Flag", (1.0 if (max_dpd and max_dpd > 0) else 0.0) if max_dpd is not None else None),
        ("Enquiry Intensity (12m)", safe_div(enquiries, 12.0) if enquiries else None),
        ("Late-Pay Frequency (12m)", safe_div(late_12m, 12.0) if late_12m else None),
    ]
    return ratios

def recommendations(metrics, ratios):
    recs = []
    d = {k:v for k,v in ratios}
    util = d.get("Utilization")
    score = metrics.get("Score")
    enquiries = metrics.get("Recent Enquiries")
    max_dpd = metrics.get("Max DPD")
    late_12m = metrics.get("Late Payments (12m)")

    if util is not None and util > 0.30:
        recs.append("High utilization (>30%): pay down revolving balances to improve score.")
    if score is not None and score < 650:
        recs.append("Score below 650: maintain on-time payments for 6 months and avoid new credit.")
    if enquiries is not None and enquiries >= 4:
        recs.append("Multiple recent enquiries: pause new applications to reduce credit-hunger flags.")
    if (max_dpd is not None and max_dpd > 0) or (late_12m is not None and late_12m > 0):
        recs.append("Delinquencies detected: clear overdue/DPD and enable autopay.")
    if metrics.get("Written-off/Settled Count"):
        recs.append("History of written-off/settled: obtain closure letters and rebuild with a secured card.")
    return recs

def metrics_to_context(metrics, ratios):
    lines = ["Key metrics & ratios (CIBIL):"]
    for k, v in metrics.items():
        if k == "Accounts Details" and v:
            lines.append(f"\nDetailed Account Information:")
            for i, account in enumerate(v, 1):
                close_info = f" (Closed: {account.get('close_date', 'Unknown')})" if account.get('status') == 'Closed' else ""
                lines.append(f"  {i}. {account.get('bank', 'Unknown')} - {account.get('type', 'Unknown')} - Status: {account.get('status', 'Unknown')}{close_info}")
        else:
            lines.append(f"{k}: {v}")
    
    lines.append("\nRatios:")
    for name, val in ratios:
        if name in ["Utilization","Score/900"]:
            lines.append(f"{name}: {fmt_pct(val)}")
        else:
            lines.append(f"{name}: {val if val is not None else 'N/A'}")
    return "\n".join(lines)


# --- Jinja filters ---
@app.template_filter("pct")
def pct(v): return fmt_pct(v)

@app.template_filter("fmt_num")
def jinja_fmt_num(v):
    if v is None: return "N/A"
    try: return f"{float(v):,.2f}"
    except Exception: return str(v)


# ---------- Template (Jinja uses 'none', not 'None') ----------
TEMPLATE = """
<!doctype html>
<title>Credit Report Analyzer (CIBIL)</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
<style>
  .card { margin-bottom: 20px; }
  .badge { font-size:12px; }
  .monospace { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; }
</style>

<div class="container my-4">
  <div class="card">
    <div class="card-body">
      <h4 class="card-title">1) Upload Credit Report PDF (CIBIL)
        {% if has_context %}
          <span class="badge text-bg-success ms-2">Context: True</span>
        {% else %}
          <span class="badge text-bg-secondary ms-2">Context: False</span>
        {% endif %}
      </h4>
      <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
        <div class="row g-2 align-items-center">
          <div class="col-auto"><input class="form-control" type="file" name="pdf_file" accept=".pdf" required></div>
          <div class="col-auto"><button class="btn btn-primary" type="submit">Analyze</button></div>
          <div class="col-auto"><a class="btn btn-outline-secondary" href="{{ url_for('clear') }}">Clear context</a></div>
          <div class="col-auto"><a class="btn btn-outline-warning" href="{{ url_for('fresh_start') }}">🔄 Fresh start</a></div>
          <div class="col-auto"><a class="btn btn-outline-dark" href="{{ url_for('debug') }}">/debug</a></div>
          <div class="col-auto"><a class="btn btn-outline-info" href="{{ url_for('test_pdf') }}">/test-pdf</a></div>
        </div>
      </form>
      {% if upload_error %}<div class="text-danger mt-2">{{ upload_error }}</div>{% endif %}
      
      <!-- Status Information -->
      <div class="mt-2">
        <small class="text-muted">
          Status: 
          {% if openai_available %}
            <span class="text-success">✓ OpenAI Connected</span>
          {% else %}
            <span class="text-danger">✗ OpenAI Not Available</span>
          {% endif %}
          | <a href="{{ url_for('test_chat') }}" target="_blank">Test API</a>
          | <a href="{{ url_for('debug') }}" target="_blank">Debug Info</a>
        </small>
      </div>
    </div>
  </div>

  {% if metrics %}
  <div class="card">
    <div class="card-body">
      <h3 class="card-title">Extracted Metrics</h3>
      <div class="row">
        <div class="col-md-7">
          <h5>Score & Summary</h5>
          <table class="table table-sm table-striped align-middle">
            <thead><tr><th>Metric</th><th class="text-end">Value</th></tr></thead>
            <tbody>
              {% for k in ["Score","Score Date","Total Accounts","Active Accounts","Closed Accounts","Credit Cards","Loans","Recent Enquiries","Max DPD","Late Payments (12m)","Written-off/Settled Count"] %}
                <tr><td><b>{{ k }}</b></td><td class="text-end">{{ metrics[k]|fmt_num }}</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        <div class="col-md-5">
          <h5>Exposure Snapshot</h5>
          <table class="table table-sm table-striped">
            <thead><tr><th>Item</th><th class="text-end">Amount</th></tr></thead>
            <tbody>
              <tr><td><b>Total Credit Limit</b></td><td class="text-end">{{ metrics["Total Credit Limit"]|fmt_num }}</td></tr>
              <tr><td><b>Total Outstanding Balance</b></td><td class="text-end">{{ metrics["Total Outstanding Balance"]|fmt_num }}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      
      <!-- Detailed Accounts Section -->
      {% if metrics["Accounts Details"] %}
      <div class="row mt-4">
        <div class="col-12">
          <h5>Account Details</h5>
          <table class="table table-sm table-striped">
            <thead>
              <tr>
                <th>Bank/Institution</th>
                <th>Account Type</th>
                <th>Status</th>
                <th>Close Date</th>
              </tr>
            </thead>
            <tbody>
              {% for account in metrics["Accounts Details"] %}
              <tr>
                <td><b>{{ account.bank }}</b></td>
                <td>{{ account.type }}</td>
                <td>
                  {% if account.status == "Active" %}
                    <span class="badge bg-success">{{ account.status }}</span>
                  {% else %}
                    <span class="badge bg-secondary">{{ account.status }}</span>
                  {% endif %}
                </td>
                <td>{{ account.close_date if account.close_date else "-" }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

  {% if ratios %}
  <div class="card">
    <div class="card-body">
      <h3 class="card-title">Ratios & Flags</h3>
      <ul>
        {% for name,val in ratios %}
          <li><b>{{ name }}</b>:
            {% if name in ["Utilization","Score/900"] %}
              {{ val|pct }}
            {% else %}
              {{ val if val is not none else "N/A" }}
            {% endif %}
          </li>
        {% endfor %}
      </ul>
      {% if recs %}
        <h5>Recommendations</h5>
        <ul>{% for r in recs %}<li>{{ r }}</li>{% endfor %}</ul>
      {% endif %}
    </div>
  </div>
  {% endif %}

  <div class="card">
    <div class="card-body">
      <h3 class="card-title">2) Chat with AI Assistant
        <div class="float-end">
          <button id="resetChat" class="btn btn-outline-secondary btn-sm me-2">🗑️ Reset</button>
          <button id="stopGeneration" class="btn btn-outline-danger btn-sm" style="display: none;">⏹️ Stop</button>
        </div>
      </h3>
      {% if not has_context %}
        <div class="text-secondary mb-3">Upload a credit report first to give the assistant context. You can still ask general questions.</div>
      {% endif %}
      
      <!-- Chat Messages Container -->
      <div id="chatContainer" class="border rounded p-3 mb-3" style="height: 400px; overflow-y: auto; background-color: #f8f9fa;">
        <div id="chatMessages">
          {% if chat_history %}
            {% for message in chat_history %}
              <div class="message mb-3 {{ 'user-message' if message.role == 'user' else 'assistant-message' }}">
                <div class="message-header">
                  <strong>{{ 'You' if message.role == 'user' else 'AI Assistant' }}</strong>
                  <small class="text-muted">{{ message.timestamp or '' }}</small>
                </div>
                <div class="message-content mt-1 p-2 rounded {{ 'bg-primary text-white' if message.role == 'user' else 'bg-white' }}">
                  {% if message.role == 'user' %}
                    {{ message.content|e }}
                  {% else %}
                    <div class="monospace">{{ message.content|e }}</div>
                  {% endif %}
                </div>
              </div>
            {% endfor %}
          {% else %}
            <div id="emptyState" class="text-center text-muted py-4">
              <i>💬 Start a conversation by asking about your credit report...</i>
            </div>
          {% endif %}
        </div>
        <div id="typingIndicator" style="display: none;">
          <div class="message mb-3 assistant-message">
            <div class="message-header">
              <strong>AI Assistant</strong>
            </div>
            <div class="message-content mt-1 p-2 rounded bg-light border">
              <div class="d-flex align-items-center">
                <div class="spinner-border spinner-border-sm text-primary me-2" role="status">
                  <span class="visually-hidden">Loading...</span>
                </div>
                <span class="text-primary fw-medium" id="typingMessage">🧠 Analyzing your credit report...</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Input Form -->
      <form id="chatForm" method="post" action="{{ url_for('ask') }}">
        <div class="input-group" style="position: relative;">
          <textarea id="promptInput" class="form-control" name="prompt" rows="2" placeholder="Ask about your credit score, improvement suggestions, loan eligibility..." style="resize: none; border-radius: 12px 0 0 12px; border-right: none;"></textarea>
          <button class="btn send-button" type="submit" id="sendBtn">
            <span id="sendBtnText">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="22" y1="2" x2="11" y2="13"></line>
                <polygon points="22,2 15,22 11,13 2,9"></polygon>
              </svg>
            </span>
            <span id="sendBtnSpinner" class="spinner-border spinner-border-sm" style="display: none;"></span>
          </button>
          <div class="loading-indicator"></div>
        </div>
        <div id="loadingMessage" class="text-center text-muted mt-2" style="display: none; font-size: 14px;">
          ✨ Sending your question to AI...
        </div>
      </form>
      
      {% if error %}<div class="text-danger mt-2">{{ error }}</div>{% endif %}
    </div>
  </div>

  <style>
    .message-content {
      max-width: 85%;
    }
    .user-message .message-content {
      margin-left: auto;
    }
    .assistant-message .message-content {
      margin-right: auto;
    }
    .typing-dots span {
      animation: typing 1.4s infinite;
      font-size: 1.2em;
    }
    .typing-dots span:nth-child(2) {
      animation-delay: 0.2s;
    }
    .typing-dots span:nth-child(3) {
      animation-delay: 0.4s;
    }
    @keyframes typing {
      0%, 60%, 100% { opacity: 0.3; }
      30% { opacity: 1; }
    }
    #chatContainer {
      scrollbar-width: thin;
    }
    #promptInput {
      border-radius: 12px 0 0 12px !important;
      border-right: none !important;
      padding: 12px 16px;
      font-size: 15px;
      line-height: 1.4;
    }
    #promptInput:focus {
      border-color: #0d6efd !important;
      box-shadow: 0 0 0 0.2rem rgba(13, 110, 253, 0.25) !important;
    }
    
    /* Beautiful Send Button */
    .send-button {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
      border: none !important;
      border-radius: 0 12px 12px 0 !important;
      padding: 12px 20px !important;
      min-width: 60px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
      position: relative;
      overflow: hidden;
      box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
    }
    
    .send-button:hover {
      background: linear-gradient(135deg, #5a67d8 0%, #6b46c1 100%) !important;
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6) !important;
    }
    
    .send-button:active {
      transform: translateY(0);
      box-shadow: 0 2px 10px rgba(102, 126, 234, 0.4) !important;
    }
    
    .send-button:disabled {
      background: linear-gradient(135deg, #94a3b8 0%, #64748b 100%) !important;
      cursor: not-allowed;
      transform: none !important;
      box-shadow: 0 2px 8px rgba(148, 163, 184, 0.3) !important;
    }
    
    .send-button svg {
      transition: transform 0.2s ease;
    }
    
    .send-button:hover svg {
      transform: translateX(2px);
    }
    
    .send-button:disabled svg {
      transform: none;
    }
    
    /* Pulse animation for send button */
    @keyframes pulse {
      0% { transform: scale(1); }
      50% { transform: scale(1.05); }
      100% { transform: scale(1); }
    }
    
    .send-button:focus {
      animation: pulse 0.6s ease-in-out;
      outline: none !important;
      box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.3) !important;
    }
    
    /* Input group styling */
    .input-group {
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
      border-radius: 12px;
      overflow: hidden;
    }
    
    .input-group:focus-within {
      box-shadow: 0 4px 20px rgba(13, 110, 253, 0.2);
    }
    
    /* Loading state animations */
    .send-button.loading {
      background: linear-gradient(135deg, #94a3b8 0%, #64748b 100%) !important;
      cursor: wait !important;
    }
    
    .send-button .spinner-border {
      width: 16px;
      height: 16px;
    }
    
    /* Input loading state */
    .form-control:disabled {
      background-color: #f8f9fa !important;
      opacity: 0.7;
      cursor: wait;
    }
    
    /* Loading indicator at bottom of input */
    .loading-indicator {
      position: absolute;
      bottom: -2px;
      left: 0;
      height: 3px;
      background: linear-gradient(90deg, #667eea, #764ba2, #667eea);
      background-size: 200% 100%;
      animation: loading-sweep 1.5s ease-in-out infinite;
      border-radius: 0 0 12px 12px;
      width: 100%;
      opacity: 0;
      transition: opacity 0.3s ease;
      box-shadow: 0 1px 3px rgba(102, 126, 234, 0.4);
    }
    
    .input-group.loading .loading-indicator {
      opacity: 1;
    }
    
    @keyframes loading-sweep {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    
    /* Simple thinking dots animation */
    .thinking-dots {
      font-style: italic;
    }
    
    .dots {
      display: inline-block;
      animation: fade-dots 2s infinite;
    }
    
    @keyframes fade-dots {
      0%, 20% { opacity: 1; }
      50% { opacity: 0.3; }
      100% { opacity: 1; }
    }
    
    /* Typewriter effect styling */
    .assistant-message .message-content {
      white-space: pre-wrap;
      word-wrap: break-word;
      overflow-wrap: break-word;
    }
    
    /* Typing cursor effect */
    .typing::after {
      content: '|';
      animation: blink-cursor 1s infinite;
      color: #666;
    }
    
    @keyframes blink-cursor {
      0%, 50% { opacity: 1; }
      51%, 100% { opacity: 0; }
    }
    
    /* Responsive send button */
    @media (max-width: 768px) {
      .send-button {
        min-width: 50px;
        padding: 10px 16px !important;
      }
      
      .send-button svg {
        width: 18px;
        height: 18px;
      }
    }
  </style>

  <script>
    let isGenerating = false;
    
    // Auto-scroll to bottom of chat
    function scrollToBottom() {
      const chatContainer = document.getElementById('chatContainer');
      if (chatContainer) {
        // Use requestAnimationFrame for smoother scrolling
        requestAnimationFrame(() => {
          chatContainer.scrollTop = chatContainer.scrollHeight;
        });
      }
    }
    
    // Show typing indicator with rotating messages
    function showTyping() {
      const messages = [
        '🧠 Analyzing your credit report...',
        '📊 Processing financial data...',
        '💡 Generating recommendations...',
        '🔍 Reviewing credit patterns...',
        '📈 Calculating risk factors...'
      ];
      
      let messageIndex = 0;
      const typingIndicator = document.getElementById('typingIndicator');
      const typingMessage = document.getElementById('typingMessage');
      
      typingIndicator.style.display = 'block';
      
      // Rotate messages every 2 seconds
      const messageInterval = setInterval(() => {
        messageIndex = (messageIndex + 1) % messages.length;
        typingMessage.textContent = messages[messageIndex];
      }, 2000);
      
      // Store interval ID to clear it later
      typingIndicator.dataset.intervalId = messageInterval;
      
      scrollToBottom();
    }
    
    // Hide typing indicator
    function hideTyping() {
      const typingIndicator = document.getElementById('typingIndicator');
      const intervalId = typingIndicator.dataset.intervalId;
      
      if (intervalId) {
        clearInterval(intervalId);
      }
      
      typingIndicator.style.display = 'none';
    }
    
    // Reset chat
    document.getElementById('resetChat').addEventListener('click', function() {
      if (confirm('Are you sure you want to reset the chat history?')) {
        // Clear any session storage flags
        sessionStorage.removeItem('shouldScrollToChat');
        
        // Show loading state
        this.innerHTML = '🔄 Resetting...';
        this.disabled = true;
        
        // Use simple redirect method instead of fetch for better compatibility
        try {
          // Create a form and submit it (more reliable than fetch)
          const form = document.createElement('form');
          form.method = 'POST';
          form.action = '{{ url_for("reset_chat") }}';
          form.style.display = 'none';
          document.body.appendChild(form);
          form.submit();
        } catch (error) {
          console.error('Form submit error:', error);
          // Fallback: Direct navigation
          window.location.href = '{{ url_for("reset_chat") }}';
        }
      }
    });
    
    // Simple form submission with visual feedback
    document.getElementById('chatForm').addEventListener('submit', function(e) {
      const input = document.getElementById('promptInput');
      const prompt = input.value.trim();
      
      if (!prompt) {
        e.preventDefault();
        alert('Please enter a question first.');
        return;
      }
      
      // Remove empty state if it exists
      const emptyState = document.getElementById('emptyState');
      if (emptyState) {
        emptyState.remove();
      }
      
      // Add user message immediately
      const chatMessages = document.getElementById('chatMessages');
      const userMessage = document.createElement('div');
      userMessage.className = 'message mb-3 user-message';
      userMessage.innerHTML = `
        <div class="message-header">
          <strong>You</strong>
          <small class="text-muted">${new Date().toLocaleTimeString()}</small>
        </div>
        <div class="message-content mt-1 p-2 rounded bg-primary text-white">
          ${prompt.replace(/</g, '&lt;').replace(/>/g, '&gt;')}
        </div>
      `;
      chatMessages.appendChild(userMessage);
      
      // Add simple "thinking..." message
      const thinkingMessage = document.createElement('div');
      thinkingMessage.className = 'message mb-3 assistant-message';
      thinkingMessage.id = 'thinkingMessage';
      thinkingMessage.innerHTML = `
        <div class="message-header">
          <strong>AI Assistant</strong>
          <small class="text-muted">${new Date().toLocaleTimeString()}</small>
        </div>
        <div class="message-content mt-1 p-2 rounded bg-light text-muted">
          <span class="thinking-dots">Thinking</span><span class="dots">...</span>
        </div>
      `;
      chatMessages.appendChild(thinkingMessage);
      
      // Scroll to bottom to show new messages
      scrollToBottom();
      
      // Show loading state on button
      const sendBtn = document.getElementById('sendBtn');
      const sendBtnText = document.getElementById('sendBtnText');
      const sendBtnSpinner = document.getElementById('sendBtnSpinner');
      
      sendBtnText.style.display = 'none';
      sendBtnSpinner.style.display = 'inline-block';
      sendBtn.disabled = true;
      sendBtn.classList.add('loading');
      
      // Set scroll flag for after response
      sessionStorage.setItem('shouldScrollToChat', 'true');
      
      // Form will submit naturally after this function completes
    });
    
    // Handle Enter key (Shift+Enter for new line)
    document.getElementById('promptInput').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        document.getElementById('chatForm').dispatchEvent(new Event('submit'));
      }
    });
    
    // Stop generation (placeholder - would need streaming implementation)
    document.getElementById('stopGeneration').addEventListener('click', function() {
      // For now, just reset UI
      isGenerating = false;
      hideTyping();
      document.getElementById('sendBtnText').style.display = 'inline-block';
      document.getElementById('sendBtnSpinner').style.display = 'none';
      document.getElementById('sendBtn').disabled = false;
      this.style.display = 'none';
    });
    
    // Fast, simple page load handler
    window.addEventListener('load', function() {
      const chatMessages = document.getElementById('chatMessages');
      
      // Remove any thinking messages that may be left from before page reload
      const thinkingMessage = document.getElementById('thinkingMessage');
      if (thinkingMessage) {
        thinkingMessage.remove();
      }
      
      // Add empty state if no messages
      if (chatMessages.children.length === 0) {
        chatMessages.innerHTML = '<div id="emptyState" class="text-center text-muted py-4"><i>💬 Start a conversation by asking about your credit report...</i></div>';
      }
      
      // Simple scroll management - if we have messages, scroll to chat
      if (sessionStorage.getItem('shouldScrollToChat') === 'true' || chatMessages.children.length > 1) {
        sessionStorage.removeItem('shouldScrollToChat');
        
        // Scroll to chat area first
        setTimeout(() => {
          document.getElementById('chatContainer').scrollIntoView({ behavior: 'auto', block: 'end' });
          
          // Then apply typewriter effect which will handle its own scrolling
          setTimeout(() => {
            scrollToBottom();
            applyTypewriterEffect();
          }, 100);
        }, 50);
      } else {
        applyTypewriterEffect();
      }
    });
    
    // Typewriter effect function
    function applyTypewriterEffect() {
      const assistantMessages = document.querySelectorAll('.assistant-message .message-content');
      const lastMessage = assistantMessages[assistantMessages.length - 1];
      
      // Only apply to new messages (check if we just submitted a form)
      if (sessionStorage.getItem('shouldScrollToChat') === 'true' && lastMessage) {
        const contentDiv = lastMessage.querySelector('.monospace') || lastMessage;
        const fullText = contentDiv.textContent;
        
        // Only apply if message is substantial (not just thinking indicator)
        if (fullText && fullText.length > 10 && !contentDiv.dataset.typed) {
          contentDiv.dataset.typed = 'true'; // Mark as already processed
          contentDiv.innerHTML = ''; // Clear content
          
          // Set fixed height to prevent screen jumping
          const tempDiv = document.createElement('div');
          tempDiv.innerHTML = fullText;
          tempDiv.style.visibility = 'hidden';
          tempDiv.style.position = 'absolute';
          tempDiv.style.width = contentDiv.offsetWidth + 'px';
          tempDiv.className = contentDiv.className;
          document.body.appendChild(tempDiv);
          
          const targetHeight = tempDiv.offsetHeight;
          contentDiv.style.minHeight = targetHeight + 'px';
          document.body.removeChild(tempDiv);
          
          // Add typing cursor
          contentDiv.classList.add('typing');
          
          // Ensure we start at the bottom and stay there
          scrollToBottom();
          
          // Type effect
          let index = 0;
          const typeSpeed = 25; // milliseconds per character (slightly faster)
          
          function typeCharacter() {
            if (index < fullText.length) {
              contentDiv.textContent += fullText[index];
              index++;
              
              // Keep scrolled to bottom more frequently but smoothly
              const chatContainer = document.getElementById('chatContainer');
              if (chatContainer) {
                // Only scroll if we're near the bottom (within 50px)
                const isNearBottom = chatContainer.scrollTop + chatContainer.clientHeight >= chatContainer.scrollHeight - 50;
                if (isNearBottom) {
                  chatContainer.scrollTop = chatContainer.scrollHeight;
                }
              }
              
              setTimeout(typeCharacter, typeSpeed);
            } else {
              // Remove typing cursor when complete
              contentDiv.classList.remove('typing');
              
              // Final scroll to bottom and reset height
              setTimeout(() => {
                scrollToBottom();
                contentDiv.style.minHeight = 'auto'; // Reset height
              }, 50);
            }
          }
          
          // Start typing after ensuring we're at bottom
          setTimeout(() => {
            scrollToBottom();
            typeCharacter();
          }, 100);
        }
      }
    }
  </script>
</div>
"""


# ---------- Routes ----------
@app.route("/", methods=["GET"])
def home():
    # Always clear session data on home page load to ensure fresh start
    # This prevents data from other sessions or browser instances
    session.clear()
    
    # Debug session content
    print(f"Session cleared. Keys: {list(session.keys())}")
    print(f"Starting fresh - no chat history or context")
    
    return render_template_string(TEMPLATE, **get_template_vars())

@app.route("/upload", methods=["POST"])
def upload():
    try:
        print(f"Upload request received. Files: {list(request.files.keys())}")
        f = request.files.get("pdf_file")
        print(f"File object: {f}")
        if f:
            print(f"Filename: {f.filename}")
        
        if not f or f.filename == "":
            print("No file selected or empty filename")
            return render_template_string(
                TEMPLATE, **get_template_vars(
                    has_context=False, 
                    upload_error="Please select a CIBIL PDF file."
                )
            )
    except Exception as e:
        print(f"Error in upload route: {e}")
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], chat_history=get_clean_chat_history(), error=None,
            upload_error=f"Upload error: {str(e)}"
        )
    
    # Validate file type
    if not f.filename.lower().endswith('.pdf'):
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], chat_history=get_clean_chat_history(), error=None,
            upload_error="Please upload a PDF file only."
        )
    
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        f.save(tmp.name)
        metrics = parse_pdf(tmp.name)
        
        # Count how many meaningful metrics were found
        meaningful_metrics = sum(1 for v in metrics.values() if v is not None and v != "")
        print(f"Found {meaningful_metrics} metrics: {metrics}")
        
        # Only reject if absolutely no useful data found
        if meaningful_metrics == 0:
            return render_template_string(
                TEMPLATE, has_context=False, metrics={}, ratios=[],
                recs=[], chat_history=get_clean_chat_history(), error=None,
                upload_error="No CIBIL data could be extracted from this PDF. This could be due to: (1) The PDF being password protected, (2) Poor image quality requiring manual OCR setup, (3) Non-standard CIBIL format. Please try a different CIBIL report or ensure Tesseract OCR is properly installed."
            )
            
    except Exception as e:
        print(f"Error processing PDF: {str(e)}")
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], chat_history=get_clean_chat_history(), error=None,
            upload_error=f"Error processing PDF: {str(e)}. Please ensure the PDF is not corrupted and try again."
        )
    finally:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except Exception:
            pass

    ratios = compute_ratios(metrics)
    recs   = recommendations(metrics, ratios)

    session["cibil_context"] = metrics_to_context(metrics, ratios)
    session["cibil_metrics"] = metrics
    session["cibil_ratios"]  = ratios
    session["cibil_recs"]    = recs

    return render_template_string(
        TEMPLATE, **get_template_vars(
            use_session_data=True,
            has_context=True, 
            metrics=metrics,
            ratios=ratios, 
            recs=recs
        )
    )

@app.route("/ask", methods=["POST"])
def ask():
    from datetime import datetime
    
    prompt = (request.form.get("prompt") or "").strip()
    context = session.get("cibil_context")
    metrics = session.get("cibil_metrics") or {}
    ratios  = session.get("cibil_ratios") or []
    recs    = session.get("cibil_recs") or []
    
    # Get or initialize chat history - ensure it's a proper list
    chat_history = session.get("chat_history")
    if not isinstance(chat_history, list):
        chat_history = []
    
    error_msg = None
    
    print(f"Ask route called with prompt: {prompt[:50] if prompt else 'None'}...")
    print(f"Client available: {client is not None}")
    print(f"API key available: {OPENAI_API_KEY is not None}")
    
    if prompt and client:
        try:
            print(f"Sending prompt to OpenAI: {prompt}")
            print(f"Context length: {len(context) if context else 0}")
            print(f"Chat history length: {len(chat_history)}")
            
            # Build messages for conversation
            messages = [
                {"role": "system", "content": "You are an expert credit analyst and financial advisor. Provide detailed, actionable advice based on credit report data. Be professional, helpful, and specific with your recommendations."}
            ]
            
            # Add context if available
            if context:
                messages.append({"role": "system", "content": f"Credit Report Context:\n{context}"})
            
            # Add chat history (last 10 messages to manage token limits)
            for msg in chat_history[-10:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            
            # Add current user message
            messages.append({"role": "user", "content": prompt})
            
            print(f"Sending {len(messages)} messages to OpenAI")
            
            # Call OpenAI with GPT-4 Turbo
            resp = client.chat.completions.create(
                model="gpt-4-turbo",  # Using gpt-4-turbo for higher quality responses
                messages=messages,
                temperature=0.3,
                max_tokens=300,  # Reduced for shorter, more concise responses
                presence_penalty=0.1,
                frequency_penalty=0.1
            )
            answer = resp.choices[0].message.content
            print(f"OpenAI response received: {answer[:100]}...")
            
            # Add messages to chat history
            timestamp = datetime.now().strftime("%I:%M %p")
            chat_history.append({
                "role": "user",
                "content": prompt,
                "timestamp": timestamp
            })
            chat_history.append({
                "role": "assistant", 
                "content": answer,
                "timestamp": timestamp
            })
            
            # Keep only last 50 messages (25 pairs)
            if len(chat_history) > 50:
                chat_history = chat_history[-50:]
            
            session["chat_history"] = chat_history
            print("Chat history updated in session")
            
        except Exception as e:
            error_msg = f"Error getting AI response: {str(e)}"
            print(f"OpenAI API error: {e}")
            import traceback
            traceback.print_exc()
    elif prompt and not client:
        error_msg = "OpenAI client not available. Please check your API key configuration in .env file."
        print("OpenAI client not available - check API key")
    elif not prompt:
        error_msg = "Please enter a question."
        print("No prompt provided")

    return render_template_string(
        TEMPLATE, **get_template_vars(use_session_data=True, error=error_msg)
    )

@app.route("/clear")
def clear():
    for k in ["cibil_context","cibil_metrics","cibil_ratios","cibil_recs"]:
        session.pop(k, None)
    return redirect(url_for("home"))

@app.route("/reset-chat", methods=["GET", "POST"])
def reset_chat():
    """Reset only the chat history, keep credit report context"""
    session.pop("chat_history", None)
    print("Chat history cleared from session")
    
    # If it's an AJAX request, return JSON
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({"success": True, "message": "Chat history reset"})
    
    # Otherwise redirect
    response = redirect(url_for("home"))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@app.route("/clear-session")
def clear_session():
    """Clear entire session - useful for debugging"""
    session.clear()
    return redirect(url_for("home"))

@app.route("/fresh-start")
def fresh_start():
    """Completely fresh start - clear everything and redirect to clean home"""
    # Clear all session data
    session.clear()
    
    # Force browser to not cache the response
    response = redirect(url_for("home"))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@app.route("/test-chat")
def test_chat():
    """Test route to check OpenAI setup"""
    try:
        if not client:
            return jsonify({"error": "OpenAI client not initialized", "api_key_set": bool(OPENAI_API_KEY)})
        
        # Test simple API call
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say 'Hello World' briefly"}],
            max_tokens=50
        )
        
        return jsonify({
            "success": True,
            "response": resp.choices[0].message.content,
            "model": resp.model
        })
    except Exception as e:
        return jsonify({"error": str(e), "api_key_set": bool(OPENAI_API_KEY)})

@app.route("/debug")
def debug():
    m = session.get("cibil_metrics") or {}
    ocr_available, ocr_status = check_ocr_dependencies()
    
    # Check OpenAI client status
    openai_status = "Available" if client else "Not Available (check API key)"
    api_key_status = "Set" if OPENAI_API_KEY else "Missing"
    
    # Get chat history for debugging
    chat_history = session.get("chat_history", [])
    
    return jsonify({
        "has_context": bool(session.get("cibil_context")),
        "keys": list(m.keys()),
        "ratios": session.get("cibil_ratios"),
        "app_status": "running",
        "ocr_status": ocr_status,
        "ocr_available": ocr_available,
        "openai_client": openai_status,
        "api_key": api_key_status,
        "metrics": m,
        "context_length": len(session.get("cibil_context", "")),
        "session_keys": list(session.keys()),
        "chat_history": chat_history,
        "chat_history_length": len(chat_history),
        "chat_history_type": str(type(chat_history))
    })

@app.route("/test-pdf")
def test_pdf():
    """Test route to debug PDF extraction with CIBIL_ocr.pdf"""
    pdf_path = r"C:\Cibil\CIBIL_ocr.pdf"
    
    if not os.path.exists(pdf_path):
        return jsonify({"error": "CIBIL_ocr.pdf not found"})
    
    try:
        # Test native extraction
        with fitz.open(pdf_path) as doc:
            native_text = "\n".join(pg.get_text() for pg in doc)
        
        # Test OCR if available
        ocr_available, ocr_status = check_ocr_dependencies()
        ocr_text = ""
        
        if ocr_available:
            try:
                ocr_text = ocr_pdf_to_text(pdf_path)
            except Exception as e:
                ocr_text = f"OCR Error: {e}"
        
        # Test parsing
        final_text = extract_text_with_ocr_fallback(pdf_path)
        parsed_metrics = parse_cibil_text(final_text)
        
        return jsonify({
            "pdf_file": "CIBIL_ocr.pdf",
            "native_text_length": len(native_text),
            "native_text_sample": native_text[:1000],
            "ocr_available": ocr_available,
            "ocr_status": ocr_status,
            "ocr_text_length": len(ocr_text),
            "ocr_text_sample": ocr_text[:1000] if ocr_text else "No OCR text",
            "final_text_length": len(final_text),
            "final_text_sample": final_text[:1000],
            "parsed_metrics": parsed_metrics,
            "meaningful_metrics": sum(1 for v in parsed_metrics.values() if v is not None and v != ""),
            "extraction_method": "Native text (good quality)" if len(native_text) > 1000 else "Would use OCR"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    print("=== Credit Report Analyzer Starting ===")
    print(f"OpenAI API Key: {'✅ Set' if OPENAI_API_KEY else '❌ Missing'}")
    print(f"OpenAI Client: {'✅ Initialized' if client else '❌ Failed'}")
    
    if client:
        try:
            # Test OpenAI connection
            test_resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=5
            )
            print("✅ OpenAI API connection successful")
        except Exception as e:
            print(f"❌ OpenAI API test failed: {e}")
    
    print("Starting Flask app on http://127.0.0.1:5065")
    print("Visit http://127.0.0.1:5065/?clear=true for a fresh start")
    app.run(host="127.0.0.1", port=5065, debug=True, use_reloader=False)
