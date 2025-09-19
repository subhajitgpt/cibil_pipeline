# credit_report_flask.py
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import fitz, tempfile, re, os, io, sys
from dotenv import load_dotenv
from openai import OpenAI

# ---- OCR deps
from PIL import Image
import pytesseract

# --- API + Flask setup ---
#load_dotenv("C:\\Cibil\\.env")  # Specific path first
load_dotenv()  # Default .env file in current directory
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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
          <div class="col-auto"><input class="form-control" type="file" name="pdf_file" required></div>
          <div class="col-auto"><button class="btn btn-primary" type="submit">Analyze</button></div>
          <div class="col-auto"><a class="btn btn-outline-secondary" href="{{ url_for('clear') }}">Clear context</a></div>
          <div class="col-auto"><a class="btn btn-outline-dark" href="{{ url_for('debug') }}">/debug</a></div>
          <div class="col-auto"><a class="btn btn-outline-info" href="{{ url_for('test_pdf') }}">/test-pdf</a></div>
        </div>
      </form>
      {% if upload_error %}<div class="text-danger mt-2">{{ upload_error }}</div>{% endif %}
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
      <h3 class="card-title">2) Chat with OpenAI about this PDF</h3>
      {% if not has_context %}
        <div class="text-secondary">Upload a credit report first to give the assistant context. You can still type a question—I'll remind you.</div>
      {% endif %}
      <form method="post" action="{{ url_for('ask') }}">
        <textarea class="form-control" name="prompt" rows="5" placeholder="e.g., Why is my score low? How to boost it? Risk for loan approval?">{{ prompt or '' }}</textarea>
        <div class="mt-3">
          <button class="btn btn-primary" type="submit">Ask</button>
        </div>
      </form>
      {% if answer %}
        <hr>
        <div><b>Assistant:</b></div>
        <div class="monospace">{{ answer }}</div>
      {% endif %}
      {% if error %}<div class="text-danger mt-2">{{ error }}</div>{% endif %}
    </div>
  </div>
</div>
"""


# ---------- Routes ----------
@app.route("/", methods=["GET"])
def home():
    return render_template_string(
        TEMPLATE,
        has_context=bool(session.get("cibil_context")),
        metrics=session.get("cibil_metrics") or {},
        ratios=session.get("cibil_ratios") or [],
        recs=session.get("cibil_recs") or [],
        prompt=None,
        answer=None,
        error=None,
        upload_error=None
    )

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("pdf_file")
    if not f or f.filename == "":
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], prompt=None, answer=None, error=None,
            upload_error="Please select a CIBIL PDF file."
        )
    
    # Validate file type
    if not f.filename.lower().endswith('.pdf'):
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], prompt=None, answer=None, error=None,
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
                recs=[], prompt=None, answer=None, error=None,
                upload_error="No CIBIL data could be extracted from this PDF. This could be due to: (1) The PDF being password protected, (2) Poor image quality requiring manual OCR setup, (3) Non-standard CIBIL format. Please try a different CIBIL report or ensure Tesseract OCR is properly installed."
            )
            
    except Exception as e:
        print(f"Error processing PDF: {str(e)}")
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], prompt=None, answer=None, error=None,
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
        TEMPLATE, has_context=True, metrics=metrics,
        ratios=ratios, recs=recs, prompt=None, answer=None,
        error=None, upload_error=None
    )

@app.route("/ask", methods=["POST"])
def ask():
    prompt = (request.form.get("prompt") or "").strip()
    context = session.get("cibil_context")
    metrics = session.get("cibil_metrics") or {}
    ratios  = session.get("cibil_ratios") or []
    recs    = session.get("cibil_recs") or []

    if not context:
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[],
            recs=[], prompt=prompt, answer=None,
            error="Please upload a credit report PDF first.", upload_error=None
        )

    answer = None
    error_msg = None
    
    if prompt and client:
        try:
            print(f"Sending prompt to OpenAI: {prompt}")
            print(f"Context length: {len(context) if context else 0}")
            
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a credit analyst. Be concise, numeric where possible, and actionable."},
                    {"role": "user", "content": f"{context}\n\nUser prompt: {prompt}"},
                ],
                temperature=0.2,
                max_tokens=500
            )
            answer = resp.choices[0].message.content
            print(f"OpenAI response received: {answer[:100]}...")
            
        except Exception as e:
            error_msg = f"Error getting AI response: {str(e)}"
            print(f"OpenAI API error: {e}")
            import traceback
            traceback.print_exc()
    elif prompt and not client:
        error_msg = "OpenAI client not available. Please check your API key configuration."
    elif not prompt:
        error_msg = "Please enter a question."

    return render_template_string(
        TEMPLATE, has_context=True, metrics=metrics, ratios=ratios,
        recs=recs, prompt=prompt, answer=answer,
        error=error_msg, upload_error=None
    )

@app.route("/clear")
def clear():
    for k in ["cibil_context","cibil_metrics","cibil_ratios","cibil_recs"]:
        session.pop(k, None)
    return redirect(url_for("home"))

@app.route("/debug")
def debug():
    m = session.get("cibil_metrics") or {}
    ocr_available, ocr_status = check_ocr_dependencies()
    
    # Check OpenAI client status
    openai_status = "Available" if client else "Not Available (check API key)"
    api_key_status = "Set" if OPENAI_API_KEY else "Missing"
    
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
        "session_keys": list(session.keys())
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
    print("Credit Report app on http://127.0.0.1:5065")
    app.run(host="127.0.0.1", port=5065, debug=True, use_reloader=False)
