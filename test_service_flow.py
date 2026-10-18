"""
One-off test: does the website's two-step flow work with an ID we choose?

The website does:
  1. GET  .../ILSServices/titleServices/<record ID>/svcId/<service ID>
  2. POST .../ILSServices/holdings/<service ID>     (returns the item counts)

This tries the two steps for "Journal of differential geometry" (QA641 .J67)
with different service IDs, to see which ones the catalog accepts.

Put this next to uf_callnumber_search.py and run:  python test_service_flow.py
"""
import random

import uf_callnumber_search as u

CALL_NUMBER = "QA641 .J67"


def title_services(record_id, service_id, token):
  url = (f"{u.HOST}/primaws/rest/priv/ILSServices/titleServices/"
         f"{record_id}/svcId/{service_id}")
  params = {"lang": "en", "hideResourceSharing": "false", "isRapido": "false",
            "record-institution": "01FALSC_UFL", "vid": u.ITEMS_VID}
  headers = dict(u.HEADERS)
  headers["Authorization"] = f"Bearer {token}"
  return u.request_with_retry("GET", url, params=params, headers=headers)


def holdings(service_id, holding, token):
  url = u.ITEMS_URL.format(service_id=service_id)
  params = {"record-institution": "01FALSC_UFL", "lang": "en", "vid": u.ITEMS_VID}
  headers = dict(u.HEADERS)
  headers["Authorization"] = f"Bearer {token}"
  headers["Content-Type"] = "application/json"
  return u.request_with_retry("POST", url, params=params, headers=headers,
                              json=u.build_items_payload(holding))


def items_size(response, hold_id):
  try:
    for loc in response.json()["data"]["itemInfo"]["locations"]:
      if str(loc.get("hold-id")) == str(hold_id):
        return loc.get("items-size")
  except Exception:
    pass
  return None


def main():
  docs, _ = u.fetch_page(CALL_NUMBER, u.build_facets(["per"]), 0, 5)
  doc = next((d for d in docs if any(h.get("callNumber") == CALL_NUMBER
                                     for h in u.marston_holdings(d))), None)
  if not doc:
    print("Could not find the journal in the search results.")
    return
  holding = next(h for h in u.marston_holdings(doc)
                 if h.get("callNumber") == CALL_NUMBER)
  record_id = holding["ilsApiId"]
  token = u.get_guest_token()
  print(f"Record ID {record_id}, holding ID {holding['holdId']}\n")

  made_up = f"54{random.randint(10**10, 10**11 - 1)}06597"
  candidates = [
      ("holding ID", holding["holdId"]),
      ("record ID", record_id),
      ("made-up 54...6597 ID", made_up),
      ("small made-up number", "12345"),
  ]

  for label, service_id in candidates:
    # Without registering first
    r = holdings(service_id, holding, token)
    plain = items_size(r, holding["holdId"])
    # Registering with titleServices first, like the website
    t = title_services(record_id, service_id, token)
    r2 = holdings(service_id, holding, token)
    registered = items_size(r2, holding["holdId"])
    returned_id = None
    try:
      returned_id = t.json()["data"]["serviceId"]
    except Exception:
      pass
    print(f"{label:<22} ({service_id})")
    print(f"   POST alone:               HTTP {r.status_code}, items-size = {plain}")
    print(f"   titleServices GET:        HTTP {t.status_code}, serviceId in reply = {returned_id}")
    print(f"   POST after titleServices: HTTP {r2.status_code}, items-size = {registered}")
    print()


if __name__ == "__main__":
  main()
