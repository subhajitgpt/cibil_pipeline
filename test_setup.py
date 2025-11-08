#!/usr/bin/env python3
"""
Simple test script to check if Flask and OpenAI are working
"""
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

print("=== Testing Environment Setup ===")
print(f"Current directory: {os.getcwd()}")
print(f"Files in current directory: {os.listdir('.')}")

# Check .env file
env_path = ".env"
if os.path.exists(env_path):
    print(f".env file exists: {env_path}")
    with open(env_path, 'r') as f:
        lines = f.readlines()
    print(f".env file has {len(lines)} lines")
else:
    print(".env file not found in current directory")

# Check API key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
print(f"API key loaded: {'Yes' if OPENAI_API_KEY else 'No'}")
if OPENAI_API_KEY:
    print(f"API key starts with: {OPENAI_API_KEY[:10]}...")

# Test OpenAI client
try:
    client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if client:
        print("OpenAI client created successfully")
        
        # Test API call
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello"}],
            max_tokens=50
        )
        print(f"API test successful: {resp.choices[0].message.content}")
    else:
        print("OpenAI client not created - no API key")
except Exception as e:
    print(f"Error with OpenAI client: {e}")

print("=== Test Complete ===")