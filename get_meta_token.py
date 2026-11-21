#!/usr/bin/env python3
"""Run this ONCE on your own computer to get the Facebook / Instagram values.

  python get_meta_token.py

It asks for three things (type or paste each, then press Enter):
  1. the App ID
  2. the App secret
  3. the short-lived token from Graph API Explorer

It then prints the three values to copy into GitHub secrets:
  META_PAGE_ID, META_PAGE_TOKEN, META_IG_USER_ID

Uses only built-in Python, so nothing needs installing.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"


def get(path, **params):
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:300]
        raise SystemExit(f"\nMeta said no: {msg}\n")


app_id = input("App ID: ").strip()
app_secret = input("App secret: ").strip()
short_token = input("Short-lived token: ").strip()

# 1. swap the short token for a long one (about 60 days)
long_user = get("oauth/access_token", grant_type="fb_exchange_token",
                client_id=app_id, client_secret=app_secret,
                fb_exchange_token=short_token)["access_token"]

# 2. a Page token made from a long user token never expires
pages = get("me/accounts", access_token=long_user, limit=50).get("data", [])
if not pages:
    raise SystemExit("\nNo Facebook Pages found for this login. In Graph API Explorer, "
                     "generate the token again and tick your Page in the permission box.\n")

print("\nPages you manage:")
for i, p in enumerate(pages, 1):
    print(f"  {i}. {p['name']}")
pick = 1
if len(pages) > 1:
    pick = int(input("Type the number of the Efe Clips Naija page: ").strip())
page = pages[pick - 1]

# 3. the Instagram account linked to that Page
info = get(page["id"], fields="instagram_business_account", access_token=page["access_token"])
ig = (info.get("instagram_business_account") or {}).get("id", "")

print("\nAdd these as GitHub secrets (copy only the part after the = sign):\n")
print("META_PAGE_ID     =", page["id"])
print("META_PAGE_TOKEN  =", page["access_token"])
if ig:
    print("META_IG_USER_ID  =", ig)
else:
    print("META_IG_USER_ID  = (none found: this Page has no Instagram account linked yet)")
