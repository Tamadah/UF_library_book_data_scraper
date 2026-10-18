"""
Search UF's catalog by title (and optionally author), limited to physical
books and journals held at Marston Science Library.

Standalone: needs only the requests library (pip install requests).

As a script:
  python marston_title_search.py "introduction to algorithms"
  python marston_title_search.py "algorithms" --author cormen
  python marston_title_search.py "journal of ecology" --match exact --items
  python marston_title_search.py "algorithms" --json results.json

From your own code:
  from marston_title_search import search_marston_by_title
  found = search_marston_by_title("introduction to algorithms", author="cormen")
  for record_id, book in found["results"].items():
      print(book["title"], [h["call_number"] for h in book["holdings"]])
"""
import argparse
import json
import os
import sys
import time

import requests

# --------------------------------------------------------------------------
# Catalog settings
# --------------------------------------------------------------------------

HOST = "https://ufl-flvc.primo.exlibrisgroup.com"
SEARCH_URL = f"{HOST}/primaws/rest/pub/pnxs"
GUEST_TOKEN_URL = f"{HOST}/primaws/rest/pub/institution/01FALSC_UFL/guestJwt"
PHYSICAL_SERVICE_URL = f"{HOST}/primaws/rest/pub/getPhysicalService/{{record_id}}"
TITLE_SERVICES_URL = (f"{HOST}/primaws/rest/priv/ILSServices/titleServices/"
                      f"{{record_id}}/svcId/{{service_id}}")
ITEMS_URL = f"{HOST}/primaws/rest/priv/ILSServices/holdings/{{service_id}}"

VID = "01FALSC_UFL:UFL"
ITEMS_VID = "01FALSC_UFL:NDE_UFL"   # the view the website uses for item lists
RECORD_OWNER = "01FALSC_NETWORK"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Marston's IDs, copied from the website's search URLs. They contain en
# dashes (–), not hyphens. Keep them exactly as they are.
MARSTON_LIBRARY_ID = "6597–112147640006597"
MARSTON_CODE = "UFSCI"

MATCH_TYPES = ("contains", "exact", "begins_with")

SEARCH_TIMEOUT = 90                 # seconds to wait for one search
REQUEST_TIMEOUT = 20                # seconds for other requests
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4                    # for 'server busy' replies
NETWORK_WAIT_LIMIT = 15 * 60        # seconds to keep waiting for a lost connection
PAUSE_BETWEEN_ITEM_REQUESTS = 0.5   # seconds, to go easy on the catalog


# --------------------------------------------------------------------------
# Reliable requests
# --------------------------------------------------------------------------

def request_with_retry(method, url, **kwargs):
  """
  Send a request, retrying temporary failures.
  - 'Server busy' replies: up to 4 tries, waiting 1s, 2s, 4s.
  - No answer at all (timeout, dropped connection, laptop asleep): keep
    trying for up to 15 minutes, waiting a little longer each time (up to
    30s), so a short outage doesn't stop the search.
  Returns the last response, or raises if the network never came back.
  """
  kwargs.setdefault("timeout", REQUEST_TIMEOUT)
  attempt = 0
  outage_started = None
  while True:
    attempt += 1
    try:
      response = requests.request(method, url, **kwargs)
      if outage_started:
        print("  Connection is back, continuing.")
      if response.status_code not in RETRY_STATUS or attempt >= MAX_ATTEMPTS:
        return response
      time.sleep(2 ** (attempt - 1))
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
      now = time.monotonic()
      if outage_started is None:
        outage_started = now
        print("  No answer from the catalog (connection lost or catalog "
              f"slow); will keep retrying for up to "
              f"{NETWORK_WAIT_LIMIT // 60} minutes...")
      if now - outage_started > NETWORK_WAIT_LIMIT:
        raise
      time.sleep(min(30, 2 ** min(attempt, 5)))


# --------------------------------------------------------------------------
# Marston filter
# --------------------------------------------------------------------------

def build_facets(location_codes=None):
  """
  The website's filters: Library = Marston, optionally only some Marston
  locations (e.g. "gen", "per"), and Held by library (physical copies only).
  """
  facets = [f"facet_library,include,{MARSTON_LIBRARY_ID}"]
  for code in location_codes or []:
    facets.append(f"facet_location_code,include,{MARSTON_LIBRARY_ID}–{code.lower()}")
  facets.append("facet_tlevel,include,available_p")
  return facets


def marston_holdings(doc):
  """The holdings on a search result that are at Marston."""
  holdings = (doc.get("delivery", {}) or {}).get("holding") or []
  return [h for h in holdings
          if (h.get("libraryCode") or h.get("library")) == MARSTON_CODE]


# --------------------------------------------------------------------------
# Item and volume counts (optional)
# --------------------------------------------------------------------------

_guest_token = None


def get_guest_token():
  """An anonymous guest token, like the website gets for visitors. Reused."""
  global _guest_token
  if _guest_token:
    return _guest_token
  params = {"isGuest": "true", "lang": "en",
            "targetUrl": f"{HOST}/discovery/search?vid={ITEMS_VID}",
            "viewId": ITEMS_VID}
  response = request_with_retry("GET", GUEST_TOKEN_URL, params=params,
                                headers=HEADERS)
  response.raise_for_status()
  token = response.text.strip().strip('"')
  if not token:
    raise LookupError("the catalog returned an empty guest token")
  _guest_token = token
  return token


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
      continue
    return response


def build_items_payload(holding, start_pos=1, count=6):
  """The request body the website sends for a holding's item list."""
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


def fetch_item_counts(doc, holding):
  """
  Count the items and volumes in one Marston holding, the same three steps
  the website takes: get a service ID, register it, ask for the items.
  Returns {"items": 47, "volumes": ["91", ..., "103"]}; raises LookupError
  if the catalog doesn't return them.
  """
  record_id = str(holding.get("ilsApiId") or "")
  if not record_id or not holding.get("holdId"):
    raise LookupError("search result is missing holding/record IDs")

  # 1. A fresh service ID for this record
  display_type = _first(doc.get("pnx", {}).get("display", {}).get("type"), "book")
  response = authed_request(
      "GET", PHYSICAL_SERVICE_URL.format(record_id=record_id),
      params={"lang": "en", "recordOwner": RECORD_OWNER,
              "sourceRecordId": record_id, "resource_type": display_type,
              "isRapido": "false", "vid": ITEMS_VID})
  service_id = response.json().get("physicalServiceId") if response.ok else None
  if not service_id:
    raise LookupError(f"no service ID (HTTP {response.status_code})")

  # 2. Register it
  response = authed_request(
      "GET", TITLE_SERVICES_URL.format(record_id=record_id, service_id=service_id),
      params={"lang": "en", "hideResourceSharing": "false", "isRapido": "false",
              "record-institution": "01FALSC_UFL", "vid": ITEMS_VID})
  if not response.ok:
    raise LookupError(f"titleServices HTTP {response.status_code}")

  # 3. The items
  response = authed_request(
      "POST", ITEMS_URL.format(service_id=service_id),
      params={"record-institution": "01FALSC_UFL", "lang": "en", "vid": ITEMS_VID},
      headers={"Content-Type": "application/json"},
      json=build_items_payload(holding))
  data = response.json().get("data") if response.ok else None
  locations = (data or {}).get("itemInfo", {}).get("locations") or []
  if not locations:
    raise LookupError(f"no item list (HTTP {response.status_code})")

  hold_id = str(holding["holdId"])
  if hold_id not in {str(loc.get("hold-id", "")) for loc in locations}:
    raise LookupError("reply was for a different holding")
  code = (holding.get("subLocationCode") or "").upper()
  items = sum(int(loc.get("items-size", 0) or 0) for loc in locations
              if loc.get("library-code") == MARSTON_CODE
              and (not code or loc.get("sub-location-code", "").upper() == code))
  volumes = data.get("possibleFilters", {}).get("volumes", []) or []
  return {"items": items, "volumes": volumes}


# --------------------------------------------------------------------------
# Title search
# --------------------------------------------------------------------------

def _first(values, default=None):
  return values[0] if values else default


def _clean(text):
  # Commas separate the parts of a catalog query, so they can't appear in a term
  return " ".join(text.replace(",", " ").split())


def build_query(title, author=None, match="contains"):
  """
  The catalog's query text, in the same form the website's advanced search
  uses: field,match,words  (several joined with ;  and AND).
  """
  if match not in MATCH_TYPES:
    raise ValueError(f"match must be one of {MATCH_TYPES}")
  parts = [f"title,{match},{_clean(title)},AND"]
  if author:
    parts.append(f"creator,contains,{_clean(author)},AND")
  return ";".join(parts)


def _fetch(query, facets, offset, limit):
  params = {
      "blendFacets": "false",
      "conjunction": "and",
      "lang": "en",
      "limit": str(limit),
      "offset": str(offset),
      "otb": "false",
      "q": query,
      "scope": "MyInstitution",
      "sort": "rank",
      "vid": VID,
      "multiFacets": "|,|".join(facets),
  }
  response = request_with_retry("GET", SEARCH_URL, params=params,
                                headers=HEADERS, timeout=SEARCH_TIMEOUT)
  response.raise_for_status()
  data = response.json()
  return data.get("docs", []), data.get("info", {}).get("total", 0)


def _holding_info(holding):
  return {
      "call_number": holding.get("callNumber", ""),
      "location": holding.get("subLocation", ""),
      "location_code": (holding.get("subLocationCode") or "").lower(),
      "status": holding.get("availabilityStatus", ""),
      "items": None,
      "volumes": None,
  }


def search_marston_by_title(title, author=None, match="contains",
                            locations=None, max_results=50, page_size=50,
                            include_items=False):
  """
  Search by title (and optionally author) for physical books and journals
  held at Marston Science Library.

  title          words from the title
  author         words from the author's name (optional)
  match          "contains" (default: all the words, anywhere in the title),
                 "exact" (the whole title), or "begins_with" (the title starts
                 with these words)
  locations      Marston location codes to keep, e.g. ["gen"] or ["per"];
                 None keeps every Marston location
  max_results    the most titles to return, in the catalog's relevance order
  include_items  also count items and volumes in each holding (3 extra
                 catalog requests per holding, so slower)

  Returns a dictionary:
    {
      "query":   {"title": ..., "author": ..., "match": ..., "locations": ...},
      "total":   titles the catalog found at Marston (may exceed what's returned),
      "count":   titles returned,
      "results": {record_id: {title, author, year, type, isbn, record_id,
                              holdings: [{call_number, location, location_code,
                                          status, items, volumes}]}}
    }
  "results" keeps the catalog's relevance order. Each title lists only its
  Marston holdings (in the chosen locations).
  """
  if not title or not title.strip():
    raise ValueError("title is required")
  query = build_query(title, author, match)
  facets = build_facets(locations)
  wanted = {c.upper() for c in (locations or [])}

  results, raw, total, offset = {}, {}, 0, 0
  while len(results) < max_results:
    docs, total = _fetch(query, facets, offset,
                         min(page_size, max_results - len(results)))
    if not docs:
      break
    for doc in docs:
      holdings = [h for h in marston_holdings(doc)
                  if not wanted or (h.get("subLocationCode") or "").upper() in wanted]
      if not holdings:
        continue
      pnx = doc.get("pnx", {})
      display = pnx.get("display", {})
      record_id = _first(pnx.get("control", {}).get("recordid"), "")
      results[record_id] = {
          "title": _first(display.get("title"), ""),
          "author": _first(display.get("creator")) or _first(display.get("contributor")),
          "year": _first(display.get("creationdate")),
          "type": _first(display.get("type")),
          "isbn": _first(pnx.get("addata", {}).get("isbn")),
          "record_id": record_id,
          "holdings": [_holding_info(h) for h in holdings],
      }
      raw[record_id] = (doc, holdings)
    offset += len(docs)
    if offset >= total:
      break

  if include_items:
    for record_id, book in results.items():
      doc, holdings = raw[record_id]
      for info, holding in zip(book["holdings"], holdings):
        try:
          counts = fetch_item_counts(doc, holding)
          info["items"] = counts["items"]
          info["volumes"] = len(counts["volumes"])
        except (requests.exceptions.RequestException, LookupError, ValueError):
          pass                       # leave as None: count unavailable
        time.sleep(PAUSE_BETWEEN_ITEM_REQUESTS)

  return {
      "query": {"title": title, "author": author, "match": match,
                "locations": locations},
      "total": total,
      "count": len(results),
      "results": results,
  }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def print_results(found):
  q = found["query"]
  what = f"title {q['match'].replace('_', ' ')} '{q['title']}'"
  if q["author"]:
    what += f", author '{q['author']}'"
  print(f"Marston Science Library: {what}")
  print(f"{found['total']} title(s) found; showing {found['count']}\n")
  for i, book in enumerate(found["results"].values(), 1):
    print(f"[{i}] {book['title']}")
    details = ", ".join(x for x in (book["author"], book["year"], book["type"]) if x)
    if details:
      print(f"    {details}")
    for h in book["holdings"]:
      line = f"    -> {h['call_number'] or '(no call number)'}  |  {h['location']}"
      if h["status"]:
        line += f"  |  {h['status']}"
      if h["items"] is not None:
        line += f"  |  {h['items']} item(s)"
      print(line)
    print("-" * 65)


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("title", help="words from the title")
  parser.add_argument("--author", help="words from the author's name")
  parser.add_argument("--match", choices=MATCH_TYPES, default="contains")
  parser.add_argument("--location", action="append", dest="locations",
                      help="Marston location code to keep (gen, per, ...); "
                           "repeat for several. Default: all locations")
  parser.add_argument("--max", type=int, default=50, help="most titles to show")
  parser.add_argument("--items", action="store_true",
                      help="also count items and volumes (slower)")
  parser.add_argument("--json", metavar="FILE",
                      help="also save the results dictionary to a JSON file")
  args = parser.parse_args()

  try:
    found = search_marston_by_title(args.title, author=args.author,
                                    match=args.match, locations=args.locations,
                                    max_results=args.max,
                                    include_items=args.items)
  except requests.exceptions.RequestException as e:
    sys.exit(f"Error connecting to the catalog: {e}")

  print_results(found)
  if args.json:
    with open(args.json, "w", encoding="utf-8") as f:
      json.dump(found, f, indent=2, ensure_ascii=False)
    print(f"Saved to {args.json}")


if __name__ == "__main__":
  main()
