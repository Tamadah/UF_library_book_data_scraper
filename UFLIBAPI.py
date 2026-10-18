import json
import os
import re
import time

import requests

SCRIPT_VERSION = "2026-10-17c (prefix search)"

HOST ="https://ufl-flvc.primo.exlibrisgroup.com"
SEARCH_URL = f"{HOST}/primaws/rest/pub/pnxs"
ITEMS_URL = f"{HOST}/primaws/rest/priv/ILSServices/holdings/{{service_id}}"
GUEST_TOKEN_URL = f"{HOST}/primaws/rest/pub/institution/01FALSC_UFL/guestJwt"

VID = "01FALSC_UFL:UFL"
ITEMS_VID = "01FALSC_UFL:NDE_UFL"   # the view the website used for the item request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# IDs copied from the website's search URLs (the mfacet=... parts).
# They contain en dashes (–), not hyphens. Keep them exactly as they are.
MARSTON_LIBRARY_ID = "6597–112147640006597"

# Marston locations to search: website location code -> label for printing.
# To add Oversize, copy its code from the website URL and add a line here.
MARSTON_LOCATIONS = {
    "gen": "General Collection",
    "per": "Periodicals",
}

LIBRARY_NAMES = {
    "UFSCI": "Marston Science Library (UFSCI)",
}

# Treat the call number as a prefix: "GC1.D25" also finds "GC1.D251x".
# Set to False to match only the exact call number you type.
PREFIX_SEARCH = True

# Item lookups: three extra requests per book. Set to False to skip them.
FETCH_ITEM_COUNTS = True
PAUSE_BETWEEN_ITEM_REQUESTS = 0.5   # seconds, to go easy on the catalog

# When an item lookup fails, the raw data is saved here so it can be checked.
DEBUG_DIR = "primo_debug"


# --------------------------------------------------------------------------
# Reliable requests
# --------------------------------------------------------------------------

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def request_with_retry(method, url, **kwargs):
  """
  Send a request, retrying temporary failures (timeouts, dropped connections,
  'server busy' replies) with a growing wait: 1s, 2s, 4s.
  Returns the last response, or raises if the network never answered.
  """
  kwargs.setdefault("timeout", 20)
  last_error = None
  for attempt in range(1, MAX_ATTEMPTS + 1):
    try:
      response = requests.request(method, url, **kwargs)
      if response.status_code not in RETRY_STATUS or attempt == MAX_ATTEMPTS:
        return response
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
      last_error = e
      if attempt == MAX_ATTEMPTS:
        raise
    time.sleep(2 ** (attempt - 1))
  raise last_error


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def normalize_call_number(call_number):
  """Uppercase and strip whitespace so 'qa76.6 .C662' matches 'QA76.6.C662'."""
  return re.sub(r"\s+", "", call_number or "").upper()


def search_term(call_number):
  """
  The catalog's "contains" search matches whole words, so "GC1.D25" would not
  find "GC1.D251x". A trailing * makes it a prefix search that finds both.
  """
  term = call_number.strip()
  if PREFIX_SEARCH and not term.endswith("*"):
    term += "*"
  return term


def build_facets(location_codes):
  """Same filters as the website: Marston + chosen locations + held by library."""
  facets = [f"facet_library,include,{MARSTON_LIBRARY_ID}"]
  for code in location_codes:
    facets.append(f"facet_location_code,include,{MARSTON_LIBRARY_ID}–{code}")
  facets.append("facet_tlevel,include,available_p")
  return facets


def fetch_page(call_number, facets, offset, limit):
  """Fetch one page of results, filtered by Primo itself (same as the website)."""
  params = {
      "blendFacets": "false",
      "conjunction": "and",
      "lang": "en",
      "limit": str(limit),
      "offset": str(offset),
      "otb": "false",
      "q": f"holding_call_number,contains,{search_term(call_number)}",
      "scope": "MyInstitution",
      "sort": "rank",
      "vid": VID,
      "multiFacets": "|,|".join(facets),
  }
  response = request_with_retry("GET", SEARCH_URL, params=params,
                                headers=HEADERS)
  response.raise_for_status()
  data = response.json()
  return data.get("docs", []), data.get("info", {}).get("total", 0)


def count_results(call_number, facets):
  """Ask Primo for the total only (fetches a single result)."""
  _, total = fetch_page(call_number, facets, offset=0, limit=1)
  return total


def marston_holdings(doc):
  """All holdings on this record that are at Marston."""
  holdings = (doc.get("delivery", {}) or {}).get("holding") or []
  return [h for h in holdings
          if (h.get("libraryCode") or h.get("library")) == "UFSCI"]


# --------------------------------------------------------------------------
# Item / volume counts
# --------------------------------------------------------------------------

_guest_token = None


def get_guest_token():
  """
  Get an anonymous guest token, the same kind the website gets for visitors
  who aren't signed in. Fetched once and reused.

  If this fails, you can paste a token from the browser's Network tab into an
  environment variable instead:  PRIMO_TOKEN=eyJ...  (it expires within a day).
  """
  global _guest_token
  if _guest_token:
    return _guest_token

  _guest_token = os.environ.get("PRIMO_TOKEN")
  if _guest_token:
    return _guest_token

  params = {
      "isGuest": "true",
      "lang": "en",
      "targetUrl": f"{HOST}/discovery/search?vid={ITEMS_VID}",
      "viewId": ITEMS_VID,
  }
  response = request_with_retry("GET", GUEST_TOKEN_URL, params=params,
                                headers=HEADERS)
  response.raise_for_status()
  token = response.text.strip().strip('"')
  if not token:
    raise LookupError("the catalog returned an empty guest token")
  _guest_token = token
  return _guest_token


PHYSICAL_SERVICE_URL = f"{HOST}/primaws/rest/pub/getPhysicalService/{{record_id}}"
TITLE_SERVICES_URL = (f"{HOST}/primaws/rest/priv/ILSServices/titleServices/"
                      f"{{record_id}}/svcId/{{service_id}}")
RECORD_OWNER = "01FALSC_NETWORK"


def authed_request(method, url, **kwargs):
  """Send a request with the guest token; get a fresh token once if it expired."""
  global _guest_token
  extra_headers = kwargs.pop("headers", {})
  for attempt in range(2):
    headers = dict(HEADERS)
    headers.update(extra_headers)
    headers["Authorization"] = f"Bearer {get_guest_token()}"
    response = request_with_retry(method, url, headers=headers, **kwargs)
    if response.status_code in (401, 403) and attempt == 0:
      _guest_token = None
      os.environ.pop("PRIMO_TOKEN", None)
      continue
    return response


def get_service_id(doc, record_id):
  """
  Step 1 of the website's item lookup: ask the catalog for a service ID for
  this record. The catalog issues a new one each time and only accepts IDs
  it issued, so this is done fresh for every record.
  """
  display_type = (doc.get("pnx", {}).get("display", {}).get("type") or ["book"])[0]
  params = {
      "lang": "en",
      "recordOwner": RECORD_OWNER,
      "sourceRecordId": record_id,
      "resource_type": display_type,
      "isRapido": "false",
      "vid": ITEMS_VID,
  }
  response = authed_request("GET", PHYSICAL_SERVICE_URL.format(record_id=record_id),
                            params=params)
  service_id = None
  if response.ok:
    try:
      service_id = response.json().get("physicalServiceId")
    except ValueError:
      pass
  if not service_id:
    path = save_debug(f"service_id_{record_id}_response.txt",
                      f"HTTP {response.status_code}\n\n{response.text}")
    raise LookupError(f"no service ID from the catalog, HTTP "
                      f"{response.status_code} (saved {path})")
  return str(service_id)


def register_service(record_id, service_id):
  """Step 2: the titleServices request the website makes before the items."""
  params = {"lang": "en", "hideResourceSharing": "false", "isRapido": "false",
            "record-institution": "01FALSC_UFL", "vid": ITEMS_VID}
  response = authed_request(
      "GET", TITLE_SERVICES_URL.format(record_id=record_id, service_id=service_id),
      params=params)
  if not response.ok:
    path = save_debug(f"title_services_{record_id}_response.txt",
                      f"HTTP {response.status_code}\n\n{response.text}")
    raise LookupError(f"titleServices HTTP {response.status_code} (saved {path})")


def build_items_payload(holding, start_pos=1, count=6):
  """
  The request body the website sends, copied from its Network tab (Payload).
  Everything in it comes from the holding in the search result. The counts we
  need (items-size, volumes) come back no matter how many items are asked for.
  """
  location = dict(holding)
  location["uniqId"] = (f"{holding.get('holdId')}-{holding.get('libraryCode')}"
                        f"-{holding.get('subLocationCode')}")
  library_name = holding.get("mainLocation", "")
  return {
      "filters": {
          "startPos": start_pos,
          "noItem": count,
          "sublibrary": library_name,
          "collection": "",
          "callnumber": "",
          "holid": holding.get("holdId", ""),
          "sublibs": library_name,
          "ilsRecordList": [{
              "institution": holding.get("organization", "01FALSC_UFL"),
              "recordId": holding.get("ilsApiId", ""),
          }],
          "holkey": holding.get("holKey", ""),
          "vid": ITEMS_VID,
          "filterCall": False,
      },
      "locations": [location],
      "hideResourceSharing": False,
  }


def save_debug(name, data):
  os.makedirs(DEBUG_DIR, exist_ok=True)
  path = os.path.join(DEBUG_DIR, name)
  with open(path, "w", encoding="utf-8") as f:
    if isinstance(data, str):
      f.write(data)
    else:
      json.dump(data, f, indent=2, ensure_ascii=False)
  return path


def fetch_item_counts(doc, holding):
  """
  Ask Primo for the items in one Marston holding, the same request the
  website makes when you open a record's location.

  Returns {"items": 47, "volumes": ["91", ..., "103"]} or None on failure.
  """
  if not holding.get("holdId") or not holding.get("ilsApiId"):
    path = save_debug("holding_without_ids.json", holding)
    raise LookupError(f"search result is missing holding/record IDs (saved {path})")

  record_id = str(holding["ilsApiId"])
  service_id = get_service_id(doc, record_id)       # step 1
  register_service(record_id, service_id)           # step 2

  # Step 3: the item request itself
  params = {"record-institution": "01FALSC_UFL", "lang": "en", "vid": ITEMS_VID}
  response = authed_request("POST", ITEMS_URL.format(service_id=service_id),
                            params=params,
                            headers={"Content-Type": "application/json"},
                            json=build_items_payload(holding))

  data = None
  if response.ok:
    try:
      data = response.json().get("data")
    except ValueError:
      data = None
  if not data or not data.get("itemInfo", {}).get("locations"):
    path = save_debug(f"items_{holding.get('holdId')}_response.txt",
                      f"HTTP {response.status_code}\n\n{response.text}")
    raise LookupError(f"HTTP {response.status_code} (saved {path})")

  locations = data.get("itemInfo", {}).get("locations", [])
  location_code = (holding.get("subLocationCode") or "").upper()

  # Make sure the reply is about the book we asked for
  wanted_hold_id = str(holding.get("holdId", ""))
  returned_ids = {str(loc.get("hold-id", "")) for loc in locations}
  if wanted_hold_id and wanted_hold_id not in returned_ids:
    path = save_debug(f"items_mismatch_{wanted_hold_id}.json", data)
    raise LookupError(f"reply was for a different holding (saved {path})")

  items = 0
  for loc in locations:
    if loc.get("library-code") != "UFSCI":
      continue
    if location_code and loc.get("sub-location-code", "").upper() != location_code:
      continue
    items += int(loc.get("items-size", 0) or 0)

  volumes = data.get("possibleFilters", {}).get("volumes", []) or []
  return {"items": items, "volumes": volumes}


def describe_volumes(volumes):
  """['91', ..., '103'] -> 'v.91–v.103 (13 volumes)'."""
  if not volumes:
    return ""
  # Read the leading number of each entry, so "52 (msg.Issue no.3" counts as 52
  numeric = sorted({int(m.group()) for v in volumes
                    if (m := re.match(r"\s*(\d+)", str(v)))})
  if numeric:
    span = (f"v.{numeric[0]}" if numeric[0] == numeric[-1]
            else f"v.{numeric[0]}–v.{numeric[-1]}")
  else:
    span = ", ".join(volumes[:5]) + (" …" if len(volumes) > 5 else "")
  word = "volume" if len(volumes) == 1 else "volumes"
  return f"{span} ({len(volumes)} {word})"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def search_by_call_number(call_number, locations=None, page_size=50,
                          max_results=2000):
  """
  Find physical books at Marston whose holding call number contains
  `call_number`, in the chosen locations (default: all in MARSTON_LOCATIONS),
  and count the items and volumes in each.
  """
  if not normalize_call_number(call_number):
    print("Please provide a call number.")
    return []

  locations = list(locations or MARSTON_LOCATIONS)
  wanted_codes = {c.upper() for c in locations}
  labels = ", ".join(MARSTON_LOCATIONS.get(c, c) for c in locations)
  facets = build_facets(locations)

  print(f"uf_callnumber_search.py, version {SCRIPT_VERSION}")
  print(f"Searching Marston ({labels}) for call number containing "
        f"'{call_number}'...\n")
  results = []
  total = 0

  try:
    offset = 0
    while offset < max_results:
      docs, total = fetch_page(call_number, facets, offset, page_size)
      if not docs:
        break

      for doc in docs:
        pnx = doc.get("pnx", {})
        display = pnx.get("display", {})
        isbn_list = pnx.get("addata", {}).get("isbn", [])

        # Keep only Marston holdings in the chosen locations; if the search
        # result doesn't include location codes, keep every Marston holding.
        holdings = marston_holdings(doc)
        chosen = [h for h in holdings
                  if (h.get("subLocationCode") or "").upper() in wanted_codes]
        holdings = chosen or holdings or [{}]

        for holding in holdings:
          results.append({
              "doc": doc,
              "holding": holding,
              "title": (display.get("title") or ["Unknown Title"])[0],
              "creator": (display.get("creator") or ["Unknown Author"])[0],
              "year": (display.get("creationdate") or ["N/A"])[0],
              "isbn": isbn_list[0] if isbn_list else None,
              "library": holding.get("libraryCode") or holding.get("library") or "",
              "sub_location": holding.get("subLocation", ""),
              "call_number": holding.get("callNumber", "N/A"),
              "status": holding.get("availabilityStatus", ""),
              "items": None,
              "volumes": [],
          })

      offset += page_size
      if offset >= total:
        break  # fetched every result Primo reported

    per_location = {code: count_results(call_number, build_facets([code]))
                    for code in locations}

  except requests.exceptions.RequestException as e:
    print(f"Error connecting to Primo VE API: {e}")
    return []

  if FETCH_ITEM_COUNTS and results:
    add_item_counts(results)

  print_results(results)
  print_summary(results, total, per_location)
  return results


def add_item_counts(results):
  print(f"Looking up items for {len(results)} holding(s)...")
  failures = 0
  for i, r in enumerate(results, 1):
    if not r["holding"]:
      continue
    try:
      counts = fetch_item_counts(r["doc"], r["holding"])
      r["items"] = counts["items"]
      r["volumes"] = counts["volumes"]
    except (requests.exceptions.RequestException, LookupError, ValueError) as e:
      failures += 1
      if failures == 1:
        print(f"  Item lookup failed for '{r['title']}': {e}")
      if failures == 3 and i == 3:
        print("  The first item lookups all failed, so skipping the rest.")
        print(f"  Send the files in the '{DEBUG_DIR}' folder to check why.")
        break
    time.sleep(PAUSE_BETWEEN_ITEM_REQUESTS)
  if failures:
    print(f"  {failures} item lookup(s) failed; those show no item count.")
  print()


def print_results(results):
  if not results:
    print("No physical books at Marston found with that call number.")
    return

  results.sort(key=lambda m: normalize_call_number(m["call_number"]))
  for idx, m in enumerate(results, 1):
    library = LIBRARY_NAMES.get(m["library"], m["library"])
    location = library + (f" -> {m['sub_location']}" if m["sub_location"] else "")

    print(f"[{idx}] {m['call_number']}")
    print(f"    {m['title']}")
    print(f"    Author: {m['creator']} ({m['year']})")
    if location:
      print(f"    -> Location: {location}")
    if m["status"]:
      print(f"    -> Status: {m['status']}")
    if m["items"] is not None:
      print(f"    -> Items: {m['items']}")
    if m["volumes"]:
      print(f"    -> Volumes: {describe_volumes(m['volumes'])}")
    if m["isbn"]:
      print(
          "    -> Thumbnail: "
          f"https://covers.openlibrary.org/b/isbn/{m['isbn']}-M.jpg?default=false"
      )
    print("-" * 65)


def print_summary(results, total, per_location):
  status_counts = {}
  for m in results:
    status = m["status"] or "unknown"
    status_counts[status] = status_counts.get(status, 0) + 1

  print("\n=== Physical books at Marston Science Library ===")
  for code, count in per_location.items():
    label = MARSTON_LOCATIONS.get(code, code)
    print(f"{label + ':':<25}{count}")
  print(f"{'All selected locations:':<25}{total}")
  if sum(per_location.values()) != total:
    print("  (The per-location numbers add up to more than the combined total"
          " when a title is held in more than one location.)")

  counted = [m for m in results if m["items"] is not None]
  if counted:
    print(f"\nItems on the shelves:    {sum(m['items'] for m in counted)}"
          f"   (from {len(counted)} of {len(results)} holdings)")
    print(f"Volumes:                 {sum(len(m['volumes']) for m in counted)}")

  print(f"\nOn shelf now:            {status_counts.get('available', 0)}")
  print(f"Not on shelf:            {status_counts.get('unavailable', 0)}")
  for status, count in status_counts.items():
    if status not in ("available", "unavailable"):
      print(f"{status + ':':<25}{count}")


if __name__ == "__main__":
  # Searches every location in MARSTON_LOCATIONS. To search just one, pass e.g.
  # search_by_call_number("TS200 .A7527", locations=["per"])
  search_by_call_number("GC1.D25")