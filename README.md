# Credit Report Analyzer - Pyodide Edition

This is a browser-based version of the Credit Report Analyzer that runs entirely in your browser using Pyodide (Python compiled to WebAssembly).

## Features

✅ **Client-side Processing**: All PDF analysis happens in your browser - no data is sent to any server  
✅ **CIBIL Report Analysis**: Extracts credit score, account details, and financial metrics  
✅ **Detailed Account Breakdown**: Shows all credit cards and loans with bank names and status  
✅ **AI-Powered Insights**: Optional OpenAI integration for personalized credit advice  
✅ **No Installation Required**: Just open in any modern browser  

## Quick Start

1. **Start the server**:
   ```bash
   python server.py
   ```

2. **Open your browser** to http://localhost:8080

3. **Upload your CIBIL PDF** and analyze

4. **Optional**: Add your OpenAI API key for AI-powered insights

## Files

- `index.html` - Main web interface
- `credit_analyzer.py` - Python analysis logic (runs in browser via Pyodide)
- `server.py` - Simple local HTTP server
- `README.md` - This file

## Key Differences from Flask Version

### ✅ Advantages:
- **Privacy**: All processing happens locally in your browser
- **No Server Required**: Runs entirely client-side
- **Cross-Platform**: Works on any device with a modern browser
- **No Installation**: No need to install Python packages

### ⚠️ Limitations:
- **OCR Not Available**: Cannot process scanned PDFs (only text-based PDFs)
- **Slower Initial Load**: Pyodide takes time to initialize
- **OpenAI API Key Required**: Must provide your own API key for AI features
- **Browser Compatibility**: Requires modern browser with WebAssembly support

## Browser Requirements

- **Chrome/Edge**: Version 86+
- **Firefox**: Version 84+
- **Safari**: Version 14.1+

## Usage Tips

1. **PDF Format**: Works best with text-based CIBIL PDFs (not scanned images)
2. **API Key**: Get your OpenAI API key from https://platform.openai.com/api-keys
3. **Privacy**: Your API key and PDF data never leave your browser
4. **Performance**: First load may take 10-30 seconds to initialize Python

## Extracted Metrics

The analyzer extracts the following information from your CIBIL report:

- **Credit Score** (handles OCR errors like "6 5A" → "654")
- **Score Date**
- **Account Summary** (Total, Active, Closed accounts)
- **Credit Cards vs Loans** breakdown
- **Detailed Account List** (Bank names, types, status)
- **Credit Limits** and **Outstanding Balances**
- **Recent Enquiries** count

## AI Features

When you provide an OpenAI API key, you can ask questions like:

- "Why is my credit score low?"
- "How can I improve my credit score?"
- "What's my loan approval risk?"
- "Which accounts should I focus on?"

## Security & Privacy

- 🔒 **No Data Transmission**: PDFs are processed locally in your browser
- 🔒 **API Key Storage**: Your OpenAI key is only used for that session
- 🔒 **No Tracking**: No analytics or data collection
- 🔒 **Local Server**: Server only serves static files, no data processing

## Troubleshooting

**"Loading Python..." stuck?**
- Check browser console for errors
- Try refreshing the page
- Ensure stable internet connection (needed to download Pyodide)

**"No CIBIL data extracted"?**
- Ensure PDF is text-based (not a scanned image)
- Check that it's a genuine CIBIL report format
- Try the /test-pdf endpoint in the Flask version first

**AI not working?**
- Verify your OpenAI API key is correct
- Check browser console for API errors
- Ensure you have API credits remaining

## Development

To modify the analysis logic, edit `credit_analyzer.py`. The file is loaded dynamically by the browser.

For UI changes, modify `index.html`.

## Converting Back to Flask

The original Flask version (`credit_report_flask.py`) has additional features like OCR support for scanned PDFs. Use that version if you need to process image-based PDFs.
