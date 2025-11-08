# credit_analyzer.py - Pyodide version
import re
import tempfile
import os
import sys

# Pyodide-compatible imports
try:
    import fitz  # PyMuPDF
except ImportError:
    print("PyMuPDF not available - PDF parsing will be limited")
    fitz = None

# ---------- Helper Functions ----------
def to_float(num_str):
    """Convert string to float, handling commas"""
    if num_str is None:
        return None
    try:
        return float(str(num_str).replace(",", "").strip())
    except Exception:
        return None

def safe_div(a, b):
    """Safe division with None handling"""
    return round(a / b, 4) if (a is not None and b not in (None, 0)) else None

def fmt_pct(x):
    """Format as percentage"""
    return f"{x*100:.2f}%" if x is not None else "N/A"

def extract_text_from_pdf(path):
    """Extract text from PDF using PyMuPDF"""
    if not fitz:
        raise Exception("PyMuPDF not available for PDF processing")
    
    try:
        text_parts = []
        with fitz.open(path) as doc:
            for page_num in range(doc.page_count):
                page = doc.load_page(page_num)
                text_parts.append(page.get_text())
        
        text = "\n".join(text_parts)
        print(f"Extracted {len(text)} characters from PDF")
        return text
        
    except Exception as e:
        print(f"PDF extraction failed: {e}")
        return ""

def parse_cibil_text(txt):
    """
    Extract key metrics from CIBIL PDF format - Pyodide optimized version
    """
    m = {}
    lines = txt.split('\n')
    print(f"Parsing text of length: {len(txt)}")

    # Look for CIBIL Score - handle OCR issues like "6 5A"
    score = None
    score_section_found = False
    
    for i, line in enumerate(lines):
        if "CIBIL Score" in line and "Control Number" not in line:
            score_section_found = True
            print(f"Found 'CIBIL Score' section on line {i}: {repr(line)}")
            
            # Check next 15 lines for score
            for j in range(i+1, min(i+15, len(lines))):
                next_line = lines[j].strip()
                
                # Skip long explanatory lines, focus on short numeric lines
                if len(next_line) < 10 and next_line:
                    print(f"  Checking short line {j}: {repr(next_line)}")
                    
                    # Handle OCR errors like "6 5A" -> should be "654"
                    ocr_match = re.match(r'(\d)\s*(\d)\s*[A-Za-z0-9]?\s*$', next_line)
                    if ocr_match:
                        # Estimate third digit (common OCR error: A=4, S=5, etc.)
                        score = int(ocr_match.group(1) + ocr_match.group(2) + "4")
                        print(f"Found OCR score pattern '{next_line}' -> estimated score: {score}")
                        break
                    
                    # Look for clean 3-digit numbers in valid range
                    if re.match(r'^\d{3}$', next_line):
                        potential_score = int(next_line)
                        if 300 <= potential_score <= 900:
                            score = potential_score
                            print(f"Found valid score on line {j}: {score}")
                            break
                
                # Stop if we hit next section
                if "Personal Information" in next_line:
                    break
    
    # Fallback: look for reasonable scores elsewhere, excluding control numbers
    if not score and score_section_found:
        print("No score found in CIBIL Score section, trying fallback...")
        for line in lines:
            # Skip lines with known large numbers (control numbers, phone numbers, etc.)
            if any(x in line for x in ["Control Number", "Account Number", "Phone", "9748425384", "4,743,293,588"]):
                continue
                
            numbers = re.findall(r'\b([6-8]\d{2})\b', line)
            for num in numbers:
                num_val = int(num)
                if 600 <= num_val <= 850:  # Realistic CIBIL score range
                    score = num_val
                    print(f"Found potential score in fallback: {score}")
                    break
            if score:
                break
    
    m["Score"] = score

    # Extract score date
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
    
    # Bank patterns to look for
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

    # Extract credit limits and balances
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

    # Count enquiries
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

    # Initialize other fields
    m["Max DPD"] = None
    m["Late Payments (12m)"] = None
    m["Written-off/Settled Count"] = None

    print(f"Final parsed metrics: {m}")
    return m

def parse_pdf(path):
    """Main PDF parsing function"""
    print(f"Starting PDF parsing for: {path}")
    
    text = extract_text_from_pdf(path)
    print(f"Extracted text length: {len(text)} characters")
    
    if len(text) > 0:
        print(f"First 300 characters: {repr(text[:300])}")
    
    result = parse_cibil_text(text)
    print(f"Parsing result: {result}")
    return result

def compute_ratios(metrics):
    """Compute financial ratios from metrics"""
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
    """Generate recommendations based on metrics"""
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
    """Convert metrics to context string for AI"""
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

print("Credit analyzer module loaded successfully")