"""
Kalshi API Endpoint Debug Script
"""

import os
import time
import base64
import requests
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# Load from environment
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")

print("="*80)
print("KALSHI API ENDPOINT DEBUGGER")
print("="*80)
print(f"API Base: {KALSHI_API_BASE}")
print(f"API Key ID: {KALSHI_API_KEY_ID[:20]}..." if KALSHI_API_KEY_ID else "None")
print("="*80)

# Load private key
def load_private_key():
    pem_base64 = KALSHI_PRIVATE_KEY_PEM_BASE64
    if not pem_base64:
        raise ValueError("KALSHI_PRIVATE_KEY_PEM_BASE64 not set")
    
    pem_bytes = base64.b64decode(pem_base64)
    return serialization.load_pem_private_key(
        pem_bytes,
        password=None,
        backend=default_backend()
    )

# Generate JWT
def generate_jwt(private_key, api_key_id):
    now = int(time.time())
    payload = {
        "iss": api_key_id,
        "iat": now,
        "exp": now + 300
    }
    
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256"
    )

# Test endpoint
def test_endpoint(base_url, endpoint, private_key, api_key_id):
    url = f"{base_url}{endpoint}"
    token = generate_jwt(private_key, api_key_id)
    
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    print(f"\nTesting: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            print(f"✓ SUCCESS")
            data = response.json()
            print(f"Response keys: {list(data.keys())}")
            return True, data
        else:
            print(f"✗ FAILED")
            print(f"Response: {response.text[:200]}")
            return False, None
            
    except Exception as e:
        print(f"✗ ERROR: {e}")
        return False, None

# Main
def main():
    try:
        private_key = load_private_key()
        print("✓ Private key loaded")
    except Exception as e:
        print(f"✗ Failed to load private key: {e}")
        return
    
    # List of common endpoints to test
    endpoints = [
        "/exchange/status",
        "/exchange/schedule",
        "/portfolio/balance",
        "/portfolio/positions",
        "/markets",
        "/series",
        "/events",
        "/trade-api/v2/exchange/status",
        "/trade-api/v2/portfolio/balance",
        "/v2/exchange/status",
        "/v2/portfolio/balance",
        "/api/v2/exchange/status",
        "/api/v2/portfolio/balance",
    ]
    
    print("\n" + "="*80)
    print("TESTING ENDPOINTS")
    print("="*80)
    
    working_endpoints = []
    
    for endpoint in endpoints:
        success, data = test_endpoint(
            KALSHI_API_BASE,
            endpoint,
            private_key,
            KALSHI_API_KEY_ID
        )
        
        if success:
            working_endpoints.append(endpoint)
        
        time.sleep(0.5)  # Be nice to the API
    
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    
    if working_endpoints:
        print(f"✓ Found {len(working_endpoints)} working endpoint(s):")
        for ep in working_endpoints:
            print(f"  - {ep}")
    else:
        print("✗ No working endpoints found")
        print("\nTrying alternative base URLs...")
        
        alt_bases = [
            "https://trading-api.kalshi.com/trade-api/v2",
            "https://api.kalshi.com/trade-api/v2",
            "https://demo-api.kalshi.co/trade-api/v2",
        ]
        
        for base in alt_bases:
            print(f"\nTrying base: {base}")
            success, data = test_endpoint(
                base,
                "/portfolio/balance",
                private_key,
                KALSHI_API_KEY_ID
            )
            if success:
                print(f"✓✓✓ THIS BASE URL WORKS: {base}")
                break

if __name__ == "__main__":
    main()
