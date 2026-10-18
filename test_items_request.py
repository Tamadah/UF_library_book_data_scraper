"""
One-off test: which part of the item request is wrong?

Tries the item request for TS200 .A7527 with every combination of
  - URL ID:  the website's (54173675370006597) vs the holding's (22376772210006597)
  - token:   the script's own guest token vs a token copied from your browser

Before running, copy a fresh token from the browser:
  Network tab -> the 54173675370006597?record-institution=... request ->
  Headers -> Request Headers -> Authorization. Copy everything after "Bearer ".
Then paste it into BROWSER_TOKEN below (keep the quotes).
"""
import requests

import uf_callnumber_search as u

BROWSER_TOKEN = "eyJraWQiOiJwcmltYVByaXZhdGVLZXktMDFGQUxTQ19VRkwiLCJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJQcmltYSIsImp0aSI6IiIsImV4cCI6MTc5MjI5NzA5OCwiaWF0IjoxNzkyMjEwNjk4LCJ1c2VyTmFtZSI6ImFub255bW91cy0xMDE3XzA0MTgxOCIsImRpc3BsYXlOYW1lIjpudWxsLCJ1c2VyIjoiYW5vbnltb3VzLTEwMTdfMDQxODE4IiwidXNlckdyb3VwIjoiR1VFU1QiLCJpbnN0aXR1dGlvbiI6IjAxRkFMU0NfVUZMIiwidXNlcklwIjoiMTI4LjIyNy41LjIzMCIsImF1dGhlbnRpY2F0aW9uUHJvZmlsZSI6IiIsImF1dGhlbnRpY2F0aW9uU3lzdGVtIjoiIiwibGFuZ3VhZ2UiOiJlbiIsInNhbWxTZXNzaW9uSW5kZXgiOiIiLCJzYW1sTmFtZUlkIjoiIiwib25DYW1wdXMiOiJmYWxzZSIsInNpZ25lZEluIjpudWxsLCJ2aWV3SWQiOiIwMUZBTFNDX1VGTDpOREVfVUZMIiwic2VsZlJlZ2lzdGVyZWQiOm51bGwsInJlc3RyaWN0ZWRVc2VyIjpudWxsfQ.GSRDDnc7epwksiXZqgHuREISVivj2qOylncJu9X9hiX4BfIZ72jBIJhjeXircyyGNp1Ec2L6a0WM0js_JcKgNg"

WEBSITE_ID = "54174160450006597"
HOLDING_ID = "22376772210006597"

HOLDING = {
    "isValidUser": True, "organization": "01FALSC_UFL", "libraryCode": "UFSCI",
    "availabilityStatus": "available", "subLocation": "Periodicals",
    "subLocationCode": "PER", "mainLocation": "UF Marston Science Library",
    "callNumber": "TS200 .A7527", "callNumberType": "0", "holdingURL": "OVP",
    "adaptorid": "ALMA_01", "ilsApiId": "990246249550306597",
    "holdId": "22376772210006597",
    "holKey": ("HoldingResultKey [mid=22376772210006597, libraryId=112147640006597,"
               " locationCode=PER, callNumber=TS200 .A7527]"),
    "matchForHoldings": [{"matchOn": "MainLocation", "holdingRecord": "852##b"}],
    "stackMapUrl": ("https://alma-apps.flvc.org/maps/locate.jsp?inst=ufl&call_number="
                    "TS200+.A7527&library_code=UFSCI&location_code=PER&location_name="
                    "Periodicals&title=Modern+casting."),
    "relatedTitle": None, "translateRelatedTitle": None, "yearFilter": None,
    "volumeFilter": None, "singleUnavailableItemProcessType": None,
    "boundWith": False, "@id": "_:2",
}


def try_request(label, service_id, token):
  headers = dict(u.HEADERS)
  headers["Authorization"] = f"Bearer {token}"
  headers["Content-Type"] = "application/json"
  params = {"record-institution": "01FALSC_UFL", "lang": "en", "vid": u.ITEMS_VID}
  try:
    r = requests.post(u.ITEMS_URL.format(service_id=service_id), params=params,
                      headers=headers, json=u.build_items_payload(HOLDING),
                      timeout=20)
  except requests.exceptions.RequestException as e:
    print(f"{label:<45} network error: {e}")
    return
  result = f"HTTP {r.status_code}"
  if r.ok:
    try:
      locs = r.json()["data"]["itemInfo"]["locations"]
      result += f"  -> items-size = {locs[0].get('items-size')}"
    except Exception:
      result += "  -> (no item list in reply)"
  else:
    result += f"  -> {r.text[:120]!r}"
  print(f"{label:<45} {result}")


if __name__ == "__main__":
  print("Getting the script's guest token...")
  try:
    script_token = u.get_guest_token()
    print(f"  got a token ({len(script_token)} characters, starts "
          f"{script_token[:12]!r})\n")
  except requests.exceptions.RequestException as e:
    script_token = None
    print(f"  failed: {e}\n")

  tokens = []
  if script_token:
    tokens.append(("script token", script_token))
  if BROWSER_TOKEN != "PASTE_TOKEN_HERE":
    tokens.append(("browser token", BROWSER_TOKEN.strip()))
  else:
    print("(No browser token pasted, so only the script's token is tested.)\n")

  for token_label, token in tokens:
    for id_label, service_id in (("website ID", WEBSITE_ID),
                                 ("holding ID", HOLDING_ID)):
      try_request(f"{id_label} + {token_label}", service_id, token)
