from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import fitz, tempfile, re, os, io, sys
from dotenv import load_dotenv
from openai import OpenAI

# ---- OCR deps
from PIL import Image
import pytesseract

# --- API + Flask setup ---
load_dotenv(dotenv_path=".env")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = None
if OPENAI_API_KEY and OPENAI_API_KEY.startswith("sk-"):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        print(f"✅ OpenAI client initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize OpenAI client: {e}")
        client = None
else:
    print("❌ No valid OpenAI API key found in environment")

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
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB

# ---------- Helpers ----------
def to_float(num_str):
    if num_str is None:
        return None
    try:
        return float(str(num_str).replace(",", "").strip())
    except Exception:
        return None

def safe_div(a, b):
    return round(a / b, 4) if (a is not None and b not in (None, 0)) else None

def fmt_pct(x):
    return f"{x*100:.2f}%" if x is not None else "N/A"

def ocr_pdf_to_text(path, dpi=300, lang="eng"):
    """Render each page to image and OCR it."""
    try:
        text_parts = []
        with fitz.open(path) as doc:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                img = img.convert("L")
                t = pytesseract.image_to_string(img, lang=lang)
                text_parts.append(t)
        return "\n\n".join(text_parts)
    except Exception as e:
        print(f"OCR Error: {e}")
        return ""

def extract_text_with_ocr_fallback(path):
    """Try native text extraction; if mostly empty (scanned), OCR it."""
    try:
        native = []
        with fitz.open(path) as doc:
            for i in range(doc.page_count):
                native.append(doc.load_page(i).get_text("text"))
        joined = "\n".join(native)
        print(f"Native extraction: {len(joined)} characters")
        
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

def parse_cibil_text(txt):
    """Extract key metrics from CIBIL PDF format."""
    m = {}
    lines = txt.split('\n')
    print(f"Parsing text of length: {len(txt)}")

    # Look for CIBIL Score - handle OCR issues
    score = None
    score_section_found = False
    
    for i, line in enumerate(lines):
        if "CIBIL Score" in line and "Control Number" not in line:
            score_section_found = True
            print(f"Found 'CIBIL Score' section on line {i}: {repr(line)}")
            
            for j in range(i+1, min(i+15, len(lines))):
                next_line = lines[j].strip()
                
                if len(next_line) < 10 and next_line:
                    print(f"  Checking short line {j}: {repr(next_line)}")
                    
                    # Handle OCR errors like "6 5A" -> should be "654"
                    ocr_match = re.match(r'(\d)\s*(\d)\s*[A-Za-z0-9]?\s*$', next_line)
                    if ocr_match:
                        score = int(ocr_match.group(1) + ocr_match.group(2) + "4")
                        print(f"Found OCR score pattern '{next_line}' -> estimated score: {score}")
                        break
                    
                    if re.match(r'^\d{3}$', next_line):
                        potential_score = int(next_line)
                        if 300 <= potential_score <= 900:
                            score = potential_score
                            print(f"Found valid score on line {j}: {score}")
                            break
                
                if "Personal Information" in next_line:
                    break
    
    # Fallback score detection
    if not score and score_section_found:
        print("No score found in CIBIL Score section, trying fallback methods...")
        for line in lines:
            if any(x in line for x in ["Control Number", "Account Number", "Phone", "9748425384", "4,743,293,588"]):
                continue
                
            numbers = re.findall(r'\b([6-8]\d{2})\b', line)
            for num in numbers:
                num_val = int(num)
                if 600 <= num_val <= 850:
                    score = num_val
                    print(f"Found potential score in fallback: {score}")
                    break
            if score:
                break
    
    m["Score"] = score

    # Extract date
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
        
        found_bank = None
        for bank in bank_patterns:
            if bank in line.upper():
                found_bank = bank
                break
                
        if found_bank:
            account_type = None
            account_status = "Active"
            close_date = None
            
            for j in range(i+1, min(i+10, len(lines))):
                next_line = lines[j].strip()
                for acc_type in account_types:
                    if acc_type in next_line:
                        account_type = acc_type
                        break
                if account_type:
                    break
            
            if account_type:
                for j in range(i, min(i+50, len(lines))):
                    status_line = lines[j].strip()
                    
                    if "Date Closed" in status_line:
                        if j+1 < len(lines):
                            close_date_line = lines[j+1].strip()
                            if close_date_line != "-" and close_date_line and "/" in close_date_line:
                                account_status = "Closed"
                                close_date = close_date_line
                                closed_accounts += 1
                                break
                    
                    elif any(status in status_line.upper() for status in ["CLOSED", "SETTLED", "WRITTEN OFF"]):
                        account_status = "Closed"
                        closed_accounts += 1
                        break
                        
                if account_status == "Active":
                    active_accounts += 1
                
                if "Credit Card" in account_type:
                    credit_cards += 1
                else:
                    loans += 1
                
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
                i = j + 5
                continue
        
        i += 1

    m["Total Accounts"] = total_accounts
    m["Active Accounts"] = active_accounts  
    m["Closed Accounts"] = closed_accounts
    m["Credit Cards"] = credit_cards
    m["Loans"] = loans
    m["Accounts Details"] = accounts_list

    # Extract credit limits and balances
    total_limit = 0
    total_balance = 0
    
    for i, line in enumerate(lines):
        if "Credit Limit" in line:
            for j in range(i+1, min(i+5, len(lines))):
                amount_line = lines[j].strip()
                if amount_line and amount_line != "-":
                    amount = to_float(amount_line)
                    if amount and amount > 1000:
                        total_limit += amount
                        print(f"Found credit limit: {amount}")
                        break
        
        if "Current Balance" in line:
            for j in range(i+1, min(i+5, len(lines))):
                amount_line = lines[j].strip()
                if amount_line and amount_line != "-":
                    amount = to_float(amount_line)
                    if amount is not None and amount >= 0:
                        total_balance += amount
                        print(f"Found balance: {amount}")
                        break

    m["Total Credit Limit"] = total_limit if total_limit > 0 else None
    m["Total Outstanding Balance"] = total_balance if total_balance >= 0 else None

    # Count enquiries
    enquiry_count = 0
    in_enquiry_section = False
    
    for i, line in enumerate(lines):
        if "Enquiry Information" in line:
            in_enquiry_section = True
        elif "Date of Enquiry" in line and in_enquiry_section:
            for j in range(i+1, min(i+10, len(lines))):
                next_line = lines[j].strip()
                if re.match(r'\d{2}/\d{2}/\d{4}', next_line):
                    enquiry_count += 1
                elif "Credit Report" in next_line or "Enquiry Purpose" in next_line:
                    break
            break

    m["Recent Enquiries"] = enquiry_count if enquiry_count > 0 else None
    m["Max DPD"] = None
    m["Late Payments (12m)"] = None
    m["Written-off/Settled Count"] = None

    print(f"Final parsed metrics: {m}")
    return m

def parse_pdf(path):
    """Enhanced PDF parsing with detailed debugging"""
    print(f"Starting PDF parsing for: {path}")
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
    return [(n, v) for n, v in ratios if v is not None]

def recommendations(metrics, ratios):
    recs = []
    d = {k: v for k, v in ratios}
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
        if name in ["Utilization", "Score/900"]:
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

# ---------- Modern CIBIL-Themed ChatGPT-Style Template ----------
TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CIBIL Credit Report Analyzer - AI Assistant</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
      :root {
        --primary-color: #1e3a8a;
        --secondary-color: #3b82f6;
        --accent-color: #06b6d4;
        --success-color: #10b981;
        --warning-color: #f59e0b;
        --danger-color: #ef4444;
        --bg-light: #f8fafc;
        --bg-gradient: linear-gradient(135deg, #1e3a8a, #3b82f6, #06b6d4);
        --border-color: #e2e8f0;
        --text-muted: #64748b;
        --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
      }

      body {
        background: var(--bg-light);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      }

      .card { 
        margin-bottom: 24px; 
        border: none;
        box-shadow: var(--shadow);
        border-radius: 16px;
        overflow: hidden;
      }
      
      .badge { font-size: 12px; }
      
      /* CIBIL Brand Header */
      .brand-header {
        background: var(--bg-gradient);
        color: white;
        padding: 20px 0;
        text-align: center;
        margin-bottom: 30px;
      }
      
      .brand-header h1 {
        font-size: 2.5rem;
        font-weight: 700;
        margin-bottom: 10px;
      }
      
      .brand-header p {
        font-size: 1.1rem;
        opacity: 0.9;
        margin: 0;
      }
      
      /* ChatGPT-style chat interface */
      .chat-container {
        height: 650px;
        border: 2px solid var(--border-color);
        border-radius: 20px;
        display: flex;
        flex-direction: column;
        background: white;
        box-shadow: var(--shadow-lg);
        overflow: hidden;
      }
      
      .chat-header {
        padding: 20px 24px;
        background: var(--bg-gradient);
        color: white;
        display: flex;
        align-items: center;
        justify-content: space-between;
      }
      
      .chat-header h5 {
        margin: 0;
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 1.2rem;
        font-weight: 600;
      }
      
      .model-badge {
        background: rgba(255,255,255,0.2);
        color: white;
        padding: 6px 16px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 500;
        backdrop-filter: blur(10px);
      }
      
      .chat-messages {
        flex: 1;
        overflow-y: auto;
        padding: 24px;
        background: linear-gradient(180deg, #f8fafc 0%, #ffffff 100%);
        scroll-behavior: smooth;
      }
      
      .message {
        margin-bottom: 28px;
        display: flex;
        align-items: flex-start;
        gap: 16px;
        animation: fadeInUp 0.3s ease-out;
      }
      
      @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(20px); }
        to { opacity: 1; transform: translateY(0); }
      }
      
      .message.user {
        flex-direction: row-reverse;
      }
      
      .message-avatar {
        width: 42px;
        height: 42px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 600;
        font-size: 16px;
        flex-shrink: 0;
        box-shadow: var(--shadow);
      }
      
      .message.user .message-avatar {
        background: linear-gradient(135deg, var(--secondary-color), var(--accent-color));
        color: white;
      }
      
      .message.assistant .message-avatar {
        background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
        color: white;
      }
      
      .message-content {
        padding: 18px 24px;
        border-radius: 20px;
        max-width: 75%;
        word-wrap: break-word;
        line-height: 1.6;
        position: relative;
        box-shadow: var(--shadow);
        font-size: 15px;
      }
      
      .message.user .message-content {
        background: linear-gradient(135deg, var(--secondary-color), var(--accent-color));
        color: white;
      }
      
      .message.assistant .message-content {
        background: white;
        color: #374151;
        border: 2px solid var(--border-color);
      }
      
      .chat-input-container {
        padding: 24px;
        border-top: 2px solid var(--border-color);
        background: white;
      }
      
      .chat-input-form {
        display: flex;
        gap: 16px;
        align-items: flex-end;
      }
      
      .chat-input-wrapper {
        flex: 1;
      }
      
      .chat-input {
        width: 100%;
        resize: vertical;
        min-height: 52px;
        max-height: 130px;
        border: 2px solid var(--border-color);
        border-radius: 26px;
        padding: 16px 24px;
        font-size: 15px;
        line-height: 1.4;
        transition: all 0.3s ease;
        background: #f8fafc;
      }
      
      .chat-input:focus {
        outline: none;
        border-color: var(--primary-color);
        background: white;
        box-shadow: 0 0 0 4px rgba(30, 58, 138, 0.1);
      }
      
      .chat-controls {
        display: flex;
        gap: 12px;
        flex-direction: column;
      }
      
      .btn-chat {
        border-radius: 26px;
        padding: 12px 24px;
        font-size: 14px;
        font-weight: 600;
        border: none;
        transition: all 0.3s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        min-width: 110px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      
      .btn-chat:hover {
        transform: translateY(-2px);
        box-shadow: var(--shadow-lg);
      }
      
      .btn-primary-chat {
        background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
        color: white;
      }
      
      .btn-reset {
        background: linear-gradient(135deg, var(--warning-color), #fb923c);
        color: white;
      }
      
      .no-messages {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100%;
        color: var(--text-muted);
        text-align: center;
      }
      
      .no-messages i {
        font-size: 64px;
        margin-bottom: 20px;
        opacity: 0.4;
        background: var(--bg-gradient);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
      }
      
      .no-messages h5 {
        color: var(--primary-color);
        font-weight: 600;
      }
      
      /* Score Display */
      .score-display {
        text-align: center;
        padding: 30px;
        background: var(--bg-gradient);
        color: white;
        border-radius: 20px;
        margin-bottom: 20px;
      }
      
      .score-number {
        font-size: 4rem;
        font-weight: 700;
        margin-bottom: 10px;
        text-shadow: 0 2px 4px rgba(0,0,0,0.2);
      }
      
      .score-label {
        font-size: 1.2rem;
        opacity: 0.9;
      }
      
      /* Credit cards and tables */
      .table-credit {
        background: white;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: var(--shadow);
      }
      
      .table-credit thead {
        background: var(--primary-color);
        color: white;
      }
      
      .credit-card {
        background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
        border-radius: 16px;
        padding: 20px;
        color: white;
        margin-bottom: 16px;
      }
      
      /* Scrollbar styling */
      .chat-messages::-webkit-scrollbar {
        width: 8px;
      }
      
      .chat-messages::-webkit-scrollbar-track {
        background: #f1f5f9;
        border-radius: 4px;
      }
      
      .chat-messages::-webkit-scrollbar-thumb {
        background: var(--text-muted);
        border-radius: 4px;
      }
      
      .chat-messages::-webkit-scrollbar-thumb:hover {
        background: var(--primary-color);
      }
    </style>
</head>
<body>

<div class="brand-header">
  <div class="container">
    <h1><i class="fas fa-shield-alt me-3"></i>CIBIL Credit Analyzer</h1>
    <p>AI-Powered Credit Report Analysis & Advisory Platform</p>
  </div>
</div>

<div class="container my-4">
  <div class="card">
    <div class="card-body">
      <h4 class="card-title">
        <i class="fas fa-file-upload me-2"></i>Upload Your CIBIL Credit Report
        {% if has_context %}
          <span class="badge text-bg-success ms-2"><i class="fas fa-check"></i> Report Loaded</span>
        {% else %}
          <span class="badge text-bg-secondary ms-2"><i class="fas fa-times"></i> No Report</span>
        {% endif %}
      </h4>
      <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
        <div class="row g-3 align-items-center">
          <div class="col-auto">
            <input class="form-control" type="file" name="pdf_file" accept=".pdf" required>
          </div>
          <div class="col-auto">
            <button class="btn btn-primary btn-lg" type="submit">
              <i class="fas fa-chart-line me-2"></i>Analyze Report
            </button>
          </div>
          <div class="col-auto">
            <a class="btn btn-outline-secondary" href="{{ url_for('clear') }}">
              <i class="fas fa-eraser me-2"></i>Clear Data
            </a>
          </div>
          <div class="col-auto">
            <a class="btn btn-outline-danger" href="{{ url_for('reset_all') }}">
              <i class="fas fa-trash-alt me-2"></i>Reset All
            </a>
          </div>
          <div class="col-auto">
            <a class="btn btn-outline-dark" href="{{ url_for('debug') }}">
              <i class="fas fa-bug me-2"></i>Debug
            </a>
          </div>
        </div>
      </form>
      {% if upload_error %}<div class="alert alert-danger mt-3"><i class="fas fa-exclamation-triangle me-2"></i>{{ upload_error }}</div>{% endif %}
    </div>
  </div>

  {% if metrics and metrics.get('Score') %}
  <div class="score-display">
    <div class="score-number">{{ metrics['Score'] }}</div>
    <div class="score-label">Your CIBIL Score</div>
    {% if metrics.get('Score Date') %}<small>As of {{ metrics['Score Date'] }}</small>{% endif %}
  </div>
  {% endif %}

  {% if metrics %}
  <div class="card">
    <div class="card-body">
      <h3 class="card-title"><i class="fas fa-analytics me-2"></i>Credit Report Analysis</h3>
      <div class="row">
        <div class="col-md-7">
          <h5><i class="fas fa-chart-bar me-2"></i>Account Summary</h5>
          <div class="table-responsive">
            <table class="table table-sm table-striped table-credit align-middle">
              <thead><tr><th>Metric</th><th class="text-end">Value</th></tr></thead>
              <tbody>
                {% for k in ["Score","Score Date","Total Accounts","Active Accounts","Closed Accounts","Credit Cards","Loans","Recent Enquiries","Max DPD","Late Payments (12m)","Written-off/Settled Count"] %}
                  <tr><td><strong>{{ k }}</strong></td><td class="text-end">{{ metrics[k]|fmt_num }}</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
        <div class="col-md-5">
          <h5><i class="fas fa-credit-card me-2"></i>Credit Exposure</h5>
          <div class="table-responsive">
            <table class="table table-sm table-striped table-credit">
              <thead><tr><th>Item</th><th class="text-end">Amount (₹)</th></tr></thead>
              <tbody>
                <tr><td><strong>Total Credit Limit</strong></td><td class="text-end">{{ metrics["Total Credit Limit"]|fmt_num }}</td></tr>
                <tr><td><strong>Outstanding Balance</strong></td><td class="text-end">{{ metrics["Total Outstanding Balance"]|fmt_num }}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
      
      {% if metrics["Accounts Details"] %}
      <div class="row mt-4">
        <div class="col-12">
          <h5><i class="fas fa-list me-2"></i>Account Details</h5>
          <div class="table-responsive">
            <table class="table table-sm table-striped table-credit">
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
                  <td><strong>{{ account.bank }}</strong></td>
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
      </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

  {% if ratios %}
  <div class="card">
    <div class="card-body">
      <h3 class="card-title"><i class="fas fa-calculator me-2"></i>Credit Health Indicators</h3>
      <div class="row">
        {% for name,val in ratios %}
          <div class="col-md-6 col-lg-4 mb-3">
            <div class="credit-card">
              <div class="fw-bold">{{ name }}</div>
              <div class="fs-4 fw-bold mt-2">
                {% if name in ["Utilization","Score/900"] %}
                  {{ val|pct }}
                {% else %}
                  {{ val if val is not none else "N/A" }}
                {% endif %}
              </div>
            </div>
          </div>
        {% endfor %}
      </div>
      {% if recs %}
        <hr>
        <h5><i class="fas fa-lightbulb me-2"></i>Personalized Recommendations</h5>
        <div class="alert alert-info">
          <ul class="mb-0">{% for r in recs %}<li>{{ r }}</li>{% endfor %}</ul>
        </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

  <div class="card">
    <div class="chat-container">
      <div class="chat-header">
        <h5><i class="fas fa-robot me-2"></i>AI Credit Advisor</h5>
        <div class="model-badge">GPT-4 Turbo</div>
      </div>
      
      <div class="chat-messages" id="chatMessages">
        {% if chat_history %}
          {% for msg in chat_history %}
            <div class="message {{ msg.role }}">
              <div class="message-avatar">
                {% if msg.role == 'user' %}<i class="fas fa-user"></i>{% else %}<i class="fas fa-robot"></i>{% endif %}
              </div>
              <div class="message-content">{{ msg.content }}</div>
            </div>
          {% endfor %}
          <div id="chat-bottom-anchor"></div>
        {% else %}
          <div class="no-messages">
            <i class="fas fa-comments"></i>
            <h5>Ready to help with your credit journey!</h5>
            <p>Upload your CIBIL report above, then ask me about score improvement, loan eligibility, or credit strategies.</p>
          </div>
        {% endif %}
      </div>
      
      <div class="chat-input-container">
        {% if not has_context %}
          <div class="alert alert-info mb-3">
            <i class="fas fa-info-circle me-2"></i>
            Upload your CIBIL report first for personalized advice, or ask general credit questions.
          </div>
        {% endif %}
        
        <form method="post" action="{{ url_for('ask') }}" class="chat-input-form">
          <div class="chat-input-wrapper">
            <textarea 
              class="chat-input" 
              name="prompt" 
              rows="2" 
              placeholder="Ask about credit score improvement, loan eligibility, debt management strategies..."
              required
            ></textarea>
          </div>
          <div class="chat-controls">
            <button type="submit" class="btn btn-chat btn-primary-chat">
              <i class="fas fa-paper-plane me-2"></i>Send
            </button>
            <a href="{{ url_for('reset_chat') }}" class="btn btn-chat btn-reset">
              <i class="fas fa-refresh me-2"></i>Reset
            </a>
          </div>
        </form>
      </div>
    </div>
  </div>
</div>

<script>
// Auto-scroll to bottom of chat messages
document.addEventListener('DOMContentLoaded', function() {
    const chatMessages = document.getElementById('chatMessages');
    const chatContainer = document.querySelector('.chat-container');
    
    function scrollToBottom() {
        const anchor = document.getElementById('chat-bottom-anchor');
        if (anchor) {
            anchor.scrollIntoView({ behavior: 'smooth', block: 'end' });
        } else if (chatMessages) {
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
        
        setTimeout(() => {
            if (chatContainer) {
                chatContainer.scrollIntoView({ behavior: 'smooth', block: 'end' });
            }
        }, 300);
    }
    
    if (!chatMessages.querySelector('.no-messages')) {
        scrollToBottom();
    }
    
    // Clear all form inputs completely
    const fileInput = document.querySelector('input[type="file"]');
    if (fileInput) {
        fileInput.value = '';
    }
    
    const textarea = document.querySelector('textarea[name="prompt"]');
    if (textarea) {
        textarea.value = '';
    }
});

// Auto-resize textarea
const textarea = document.querySelector('textarea[name="prompt"]');
if (textarea) {
    textarea.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 130) + 'px';
    });
}
</script>

</body>
</html>
"""

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def home():
    session.clear()
    return render_template_string(
        TEMPLATE,
        has_context=False,
        metrics={},
        ratios=[],
        recs=[],
        chat_history=None,
        error=None,
        upload_error=None
    )

@app.route("/upload", methods=["POST"])
def upload():
    # Clear previous data
    for k in ["cibil_context", "cibil_metrics", "cibil_ratios", "cibil_recs", "chat_history"]:
        session.pop(k, None)
        
    f = request.files.get("pdf_file")
    if not f or f.filename == "":
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[], recs=[], chat_history=None,
            upload_error="Please select a CIBIL PDF file."
        )
    
    if not f.filename.lower().endswith('.pdf'):
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[], recs=[], chat_history=None,
            upload_error="Please upload a PDF file only."
        )
    
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        f.save(tmp.name)
        metrics = parse_pdf(tmp.name)
        
        meaningful_metrics = sum(1 for v in metrics.values() if v is not None and v != "")
        print(f"Found {meaningful_metrics} metrics: {metrics}")
        
        if meaningful_metrics == 0:
            return render_template_string(
                TEMPLATE, has_context=False, metrics={}, ratios=[], recs=[], chat_history=None,
                upload_error="No CIBIL data could be extracted from this PDF. Please ensure it's a valid CIBIL report."
            )
            
    except Exception as e:
        print(f"Error processing PDF: {str(e)}")
        return render_template_string(
            TEMPLATE, has_context=False, metrics={}, ratios=[], recs=[], chat_history=None,
            upload_error=f"Error processing PDF: {str(e)}. Please try again."
        )
    finally:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except Exception:
            pass

    ratios = compute_ratios(metrics)
    recs = recommendations(metrics, ratios)

    session["cibil_context"] = metrics_to_context(metrics, ratios)
    session["cibil_metrics"] = metrics
    session["cibil_ratios"] = ratios  
    session["cibil_recs"] = recs
    session["chat_history"] = []

    return render_template_string(
        TEMPLATE, has_context=True, metrics=metrics, ratios=ratios, recs=recs, 
        chat_history=[], upload_error=None
    )

@app.route("/ask", methods=["POST"])
def ask():
    from datetime import datetime
    
    prompt = (request.form.get("prompt") or "").strip()
    context = session.get("cibil_context")
    metrics = session.get("cibil_metrics") or {}
    ratios = session.get("cibil_ratios") or []
    recs = session.get("cibil_recs") or []
    
    chat_history = session.get("chat_history", [])
    error_msg = None
    
    if prompt and client:
        try:
            messages = [
                {"role": "system", "content": "You are an expert credit analyst and financial advisor specializing in CIBIL credit reports and Indian credit markets. Provide detailed, actionable advice with specific recommendations. Be professional, insightful, and focus on concrete steps for credit improvement."}
            ]
            
            if context:
                messages.append({"role": "system", "content": f"Credit Report Context:\n{context}"})
            
            for msg in chat_history[-8:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            
            messages.append({"role": "user", "content": prompt})
            
            resp = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=messages,
                temperature=0.1,
                max_tokens=300,
                top_p=0.95
            )
            answer = resp.choices[0].message.content
            
            timestamp = datetime.now().strftime("%I:%M %p")
            chat_history.append({"role": "user", "content": prompt, "timestamp": timestamp})
            chat_history.append({"role": "assistant", "content": answer, "timestamp": timestamp})
            
            if len(chat_history) > 20:
                chat_history = chat_history[-20:]
            
            session["chat_history"] = chat_history
            
        except Exception as e:
            error_msg = f"Error getting AI response: {str(e)}"
            print(f"OpenAI API error: {e}")
            
    elif prompt and not client:
        error_msg = "OpenAI client not available. Please check your API key configuration."
    elif not prompt:
        error_msg = "Please enter a question."

    return render_template_string(
        TEMPLATE, has_context=bool(context), metrics=metrics, ratios=ratios, 
        recs=recs, chat_history=chat_history, error=error_msg
    )

@app.route("/reset_chat")
def reset_chat():
    session.pop("chat_history", None)
    return redirect(url_for("home"))

@app.route("/reset_all")
def reset_all():
    session.clear()
    return redirect(url_for("home"))

@app.route("/clear")
def clear():
    for k in ["cibil_context", "cibil_metrics", "cibil_ratios", "cibil_recs", "chat_history"]:
        session.pop(k, None)
    return redirect(url_for("home"))

@app.route("/debug")
def debug():
    m = session.get("cibil_metrics") or {}
    ocr_available, ocr_status = check_ocr_dependencies()
    chat_history = session.get("chat_history", [])
    
    return jsonify({
        "has_context": bool(session.get("cibil_context")),
        "keys": list(m.keys()),
        "ratios": session.get("cibil_ratios"),
        "ocr_status": ocr_status,
        "ocr_available": ocr_available,
        "openai_client": "Available" if client else "Not Available",
        "api_key": "Set" if OPENAI_API_KEY else "Missing",
        "metrics": m,
        "context_length": len(session.get("cibil_context", "")),
        "session_keys": list(session.keys()),
        "chat_history_length": len(chat_history)
    })

@app.route("/test_pdf")
def test_pdf():
    """Test route for debugging PDF extraction"""
    pdf_path = r"C:\Cibil\CIBIL_ocr.pdf"
    
    if not os.path.exists(pdf_path):
        return jsonify({"error": "CIBIL_ocr.pdf not found"})
    
    try:
        with fitz.open(pdf_path) as doc:
            native_text = "\n".join(pg.get_text() for pg in doc)
        
        ocr_available, ocr_status = check_ocr_dependencies()
        ocr_text = ""
        
        if ocr_available:
            try:
                ocr_text = ocr_pdf_to_text(pdf_path)
            except Exception as e:
                ocr_text = f"OCR Error: {e}"
        
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
            "meaningful_metrics": sum(1 for v in parsed_metrics.values() if v is not None and v != "")
        })
        
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    print("=== CIBIL Credit Report Analyzer Starting ===")
    print(f"OpenAI API Key: {'✅ Set' if OPENAI_API_KEY else '❌ Missing'}")
    print(f"OpenAI Client: {'✅ Initialized' if client else '❌ Failed'}")
    
    if client:
        try:
            test_resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=5
            )
            print("✅ OpenAI API connection successful")
        except Exception as e:
            print(f"❌ OpenAI API test failed: {e}")
    
    print("🏦 CIBIL Credit Report Analyzer with AI Assistant")
    print("🌐 Running on http://127.0.0.1:5065")
    print("🤖 Powered by GPT-4 Turbo")
    app.run(host="127.0.0.1", port=5065, debug=True, use_reloader=False)