# Simple HTTP Server for Pyodide App
# Run this file to serve the Pyodide credit analyzer locally

import http.server
import socketserver
import webbrowser
import os
from pathlib import Path

PORT = 8080
DIRECTORY = Path(__file__).parent

class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)
    
    def end_headers(self):
        # Add CORS headers to allow cross-origin requests (needed for OpenAI API)
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        super().end_headers()

def run_server():
    """Start the HTTP server and open browser"""
    try:
        with socketserver.TCPServer(("", PORT), CustomHTTPRequestHandler) as httpd:
            print(f"🚀 Credit Report Analyzer (Pyodide) Server Started")
            print(f"📍 Server running at: http://localhost:{PORT}")
            print(f"📂 Serving from: {DIRECTORY}")
            print(f"🌐 Opening browser...")
            print(f"")
            print(f"Features:")
            print(f"  ✅ Client-side PDF processing (no data leaves your browser)")
            print(f"  ✅ CIBIL credit report analysis")
            print(f"  ✅ Detailed account breakdown")
            print(f"  ✅ AI-powered insights (requires OpenAI API key)")
            print(f"")
            print(f"Press Ctrl+C to stop the server")
            
            # Open browser
            webbrowser.open(f"http://localhost:{PORT}")
            
            # Start serving
            httpd.serve_forever()
            
    except KeyboardInterrupt:
        print(f"\n🛑 Server stopped by user")
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"❌ Port {PORT} is already in use. Try a different port or stop the existing server.")
        else:
            print(f"❌ Server error: {e}")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")

if __name__ == "__main__":
    # Check if required files exist
    required_files = ["index.html", "credit_analyzer.py"]
    missing_files = [f for f in required_files if not (DIRECTORY / f).exists()]
    
    if missing_files:
        print(f"❌ Missing required files: {missing_files}")
        print(f"Make sure you're running this from the correct directory.")
        exit(1)
    
    run_server()