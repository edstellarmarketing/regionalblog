"""
Test script: Push a single embed block to the 'test' blog post in Webflow.
Run: python test_push.py YOUR_API_TOKEN
"""
import requests
import sys
import json

API_BASE = "https://api.webflow.com/v2"
COLLECTION_ID = "64ac3a242208dda62b6e6a90"

if len(sys.argv) < 2:
    print("Usage: python test_push.py YOUR_API_TOKEN")
    sys.exit(1)

TOKEN = sys.argv[1]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "accept": "application/json",
}

# ── Step 1: Find the 'test' blog post ──
print("🔍 Searching for 'test' blog post...")
resp = requests.get(
    f"{API_BASE}/collections/{COLLECTION_ID}/items",
    headers=HEADERS,
    params={"limit": 100}
)
data = resp.json()
test_item = None
for item in data.get("items", []):
    if item["fieldData"].get("slug") == "test" or item["fieldData"].get("name") == "test":
        test_item = item
        break

if not test_item:
    print("❌ Could not find 'test' blog post. Available slugs:")
    for item in data.get("items", [])[:10]:
        print(f"   - {item['fieldData'].get('slug')} → {item['fieldData'].get('name')}")
    sys.exit(1)

item_id = test_item["id"]
item_name = test_item["fieldData"].get("name")
print(f"✅ Found: {item_name} (ID: {item_id})")

# ── Step 2: Push test content ──
# Test with JUST the embed-wrapped takeaway block
test_content = '''<div data-rt-embed-type="html">
<div class='takeaway'>  <p>💡 KEY TAKEAWAYS</p>  <ul>    <li>      Edstellar is the best corporate training company in New Zealand with 2,000+ corporate training courses in NZ and 5,000+ trainers across technical, leadership, and behavioural domains.    </li>    <li>      Lumify Work is New Zealand's largest corporate IT training provider and Microsoft NZ's most strategic Learning Partner, training 5,000+ students per year.    </li>    <li>      Skillset NZ stands out for its exclusively B2B model serving large and medium organisations for 30+ years, with verified clients including WorkSafe NZ.    </li>    <li>      Companies were evaluated on trainer quality, NZQA and regulatory alignment, SME and geographic reach beyond Auckland, and post-training support.    </li>  </ul></div>
</div>'''

print(f"\n📤 Pushing test content ({len(test_content)} chars)...")
print(f"   Content preview: {test_content[:100]}...")

payload = {
    "items": [{
        "id": item_id,
        "fieldData": {
            "content": test_content
        }
    }]
}

resp = requests.patch(
    f"{API_BASE}/collections/{COLLECTION_ID}/items",
    headers=HEADERS,
    json=payload
)

print(f"\n📡 Response: HTTP {resp.status_code}")
print(json.dumps(resp.json(), indent=2))

if resp.status_code == 200:
    print("\n✅ SUCCESS! Check the 'test' blog post in Webflow CMS editor.")
    print("   Look for: embed block with takeaway content")
else:
    print(f"\n❌ FAILED: {resp.status_code}")
