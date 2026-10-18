"""
Count the physical items on each shelf of Marston's Periodicals.

Input:  a CSV with one row per shelf, in shelf order:
          shelf,start,end
          1,QA1,QA76.6
          2,QA76.65,QA299
          ...
        start and end are LC call numbers; both ends are included, at the
        detail you write them ("QA299" covers QA299.5 .B2 too).

        An optional 4th column, length, gives each shelf's length in any unit
        (feet, metres, ...); the counts are then also divided by it.

Output: shelf_counts.csv        titles and physical items per shelf
        holdings_detail.csv     one row per holding: call number, shelf, items
        shelf_distribution.png  bar chart per shelf (and per unit length)

Run:    python shelf_density.py shelves.csv
        python shelf_density.py shelves.csv --titles-only   (fast, no item counts)

Needs uf_callnumber_search.py in the same folder.

How it works
  1. Works out the fewest prefix searches (QA1*, QA76*, QB*...) that cover
     every shelf, including shelves that cross into the next class.
  2. Runs them against the catalog, splitting any search too big to read in
     full (more than 500 titles) into narrower ones, so nothing is missed.
     Searches are split by class number, then by year if the shelf list has
     years in it (dissertations: LD1780 1913 ...), then by cutter letter.
  3. Puts each call number on the shelf whose range contains it, in LC
     shelf order. Each holding lands on exactly one shelf.
  4. Counts physical items (every bound volume, issue and copy) for each
     holding. Counts are saved as they come in, so if the run stops, running
     it again picks up where it left off.
"""
import argparse
import csv
import inspect
import json
import os
import re
import string
import sys
import time

import requests

import uf_callnumber_search as u

LOCATIONS = ["gen"]                 # Periodicals only
OUTPUT_FILE = "shelf_counts_gen.csv"
DETAIL_FILE = "holdings_detail_gen.csv"
CHART_FILE = "shelf_distribution_gen.png"
CACHE_FILE = "item_counts_cache_gen.json"
SEARCH_CACHE_FILE = "search_cache_gen.json"
SEARCH_CACHE_MAX_AGE = 2 * 24 * 3600   # reuse finished searches for 2 days
PAUSE_BETWEEN_SEARCHES = 1.0           # seconds
MAX_READABLE = 500                  # the catalog won't page past 500 results
MAX_SEARCHES = 400                  # safety limit for very broad shelf lists
MAX_CLASS_DIGITS = 4                # LC class numbers go up to 4 digits


# --------------------------------------------------------------------------
# LC call numbers, including years (dissertations: LD1780 1973 .S714)
# --------------------------------------------------------------------------

LC_PATTERN = re.compile(r"^\s*([A-Z]{1,3})\s*(\d+)(?:\.(\d+))?(.*)$")
CUTTER_PATTERN = re.compile(r"^\s*\.?\s*([A-Z])(\d+)")
YEAR_PATTERN = re.compile(r"^\s*(\d{4})(?!\d)[A-Z]?")
YEAR = "#"          # marks a year among the parts, e.g. ("#", "1973")


def parse_lc(call_number):
  """
  Split an LC call number into the parts that decide shelf order:
    "QA76.65 .C662 2009"  -> ("QA", "76", "65", [("C", "662"), ("#", "2009")])
    "LD1780 1973 .S714"   -> ("LD", "1780", "", [("#", "1973"), ("S", "714")])
  After the class number come cutters (letter + digits) and 4-digit years,
  in the order they appear. Returns None if it isn't an LC call number.
  """
  m = LC_PATTERN.match((call_number or "").upper())
  if not m:
    return None
  letters, whole, decimal, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4)
  parts = []
  while True:
    c = CUTTER_PATTERN.match(rest)
    if c:
      parts.append((c.group(1), c.group(2)))
      rest = rest[c.end():]
      continue
    y = YEAR_PATTERN.match(rest)
    if y:
      parts.append((YEAR, y.group(1)))
      rest = rest[y.end():]
      continue
    break
  return letters, whole, decimal, parts


def lc_key(parts, decimal_digits=None, cutters=None, cutter_digits=None):
  """
  Shelf-order key. Class decimals and cutter numbers are compared as decimal
  fractions (.65 comes after .6, D251 after D25); years compare as numbers and
  come before cutters in the same position. The optional limits cut the key
  down to a range end's precision, so an end of "QA76" covers QA76.9 too.
  """
  letters, whole, decimal, cuts = parts
  if decimal_digits is not None:
    decimal = decimal[:decimal_digits]
  key = [letters, int(whole), decimal.ljust(12, "0")]
  if cutters is not None:
    cuts = cuts[:cutters]
  for i, (letter, digits) in enumerate(cuts):
    if letter == YEAR:
      key += ["0", digits.zfill(12)]          # a year sorts before a cutter
      continue
    if cutter_digits is not None and i < len(cutter_digits):
      digits = digits[:cutter_digits[i]]
    key += ["1" + letter, digits.ljust(12, "0")]
  return key


def lc_sort_key(call_number):
  parts = parse_lc(call_number)
  return (0, lc_key(parts)) if parts else (1, [u.normalize_call_number(call_number)])


def in_lc_range(call_number, start, end):
  """True if call_number is shelved between start and end, both inclusive."""
  parts = parse_lc(call_number)
  if not parts:
    return False
  if lc_key(parts) < lc_key(start):
    return False
  # Compare at the end's precision: an end of "GC1 .D25" includes "GC1 .D251x"
  end_letters, end_whole, end_decimal, end_cutters = end
  limited = lc_key(parts,
                   decimal_digits=len(end_decimal),
                   cutters=len(end_cutters),
                   cutter_digits=[len(d) for _, d in end_cutters])
  return limited <= lc_key(end)


# --------------------------------------------------------------------------
# 1. Reading the shelf list
# --------------------------------------------------------------------------

def read_shelves(path):
  shelves = []
  with open(path, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    columns = {c.strip().lower(): c for c in (reader.fieldnames or [])}
    missing = {"shelf", "start", "end"} - set(columns)
    if missing:
      sys.exit(f"{path} needs the columns shelf,start,end "
               f"(missing: {', '.join(sorted(missing))})")
    for line_no, row in enumerate(reader, 2):
      name = row[columns["shelf"]].strip()
      start = row[columns["start"]].strip()
      end = row[columns["end"]].strip()
      if not (name or start or end):
        continue
      start_parts, end_parts = parse_lc(start), parse_lc(end)
      if not start_parts or not end_parts:
        sys.exit(f"{path} line {line_no}: '{start}' or '{end}' isn't an LC "
                 f"call number")
      if lc_key(start_parts) > lc_key(end_parts):
        sys.exit(f"{path} line {line_no}: start '{start}' comes after end "
                 f"'{end}' in shelf order")
      length = None
      if "length" in columns and row[columns["length"]].strip():
        try:
          length = float(row[columns["length"]])
        except ValueError:
          sys.exit(f"{path} line {line_no}: length "
                   f"'{row[columns['length']]}' isn't a number")
        if length <= 0:
          sys.exit(f"{path} line {line_no}: length must be more than 0")
      shelves.append({"shelf": name, "start": start, "end": end,
                      "length": length,
                      "start_parts": start_parts, "end_parts": end_parts})
  if not shelves:
    sys.exit(f"{path} has no shelves")
  return shelves


# --------------------------------------------------------------------------
# 2. Planning the searches
# --------------------------------------------------------------------------

def numbers_with_prefix(prefix, max_len):
  """Count of class numbers (up to max_len digits) that start with prefix."""
  return sum(10 ** k for k in range(max_len - len(prefix) + 1))


def numbers_in_range_with_prefix(prefix, lo, hi, max_len):
  count = 0
  for k in range(max_len - len(prefix) + 1):
    first = int(prefix) * 10 ** k
    last = first + 10 ** k - 1
    count += max(0, min(last, hi) - max(first, lo) + 1)
  return count


def digit_cover(lo, hi):
  """
  Few prefixes whose matches include every class number from lo to hi.
  A prefix is used as-is when most of what it matches is in range (the
  extra matches are filtered out later); otherwise it's narrowed.
  Returns (digits, wildcard) pairs: ("76", True) means search "76*".
  """
  max_len = max(len(str(hi)), len(str(lo))) if hi is not None else MAX_CLASS_DIGITS
  hi = hi if hi is not None else 10 ** MAX_CLASS_DIGITS - 1

  def cover(prefix):
    total = numbers_with_prefix(prefix, max_len)
    inside = numbers_in_range_with_prefix(prefix, lo, hi, max_len)
    if inside == 0:
      return []
    # Use the wildcard when the number itself is in range (so decimals like
    # QA2.5 are included), or when most of its matches are in range anyway.
    if (lo <= int(prefix) <= hi or inside * 2 >= total
        or len(prefix) >= max_len):
      return [(prefix, True)]
    out = []
    for d in string.digits:
      out += cover(prefix + d)
    return out

  result = []
  for d in "123456789":
    result += cover(d)
  return result


def whole_class_searches(start_letters, end_letters):
  """
  Searches for every class strictly between two classes, one per class:
    QA..QD -> QB*, QC*        AM..HD -> AN*..AZ*, B*, C*, ..., G*, H1..9*, HA*..HC*
  A first letter entirely inside the span is one search (B* covers BA..BZ).
  A one-letter class next to a boundary (H, between G and HD) uses H1*..H9*,
  so it doesn't also pull in HD, HE, ...
  Returns (letters, digits, wildcard) triples.
  """
  out = []
  first_a, first_b = start_letters[0], end_letters[0]
  for first in string.ascii_uppercase:
    if first_a < first < first_b:
      out.append((first, "", True))            # the whole letter, e.g. B*
      continue
    if first not in (first_a, first_b):
      continue
    if start_letters < first < end_letters:     # one-letter class, e.g. H or Q
      out += [(first, d, True) for d in "123456789"]
    for second in string.ascii_uppercase:
      cls = first + second
      if start_letters < cls < end_letters:
        out.append((cls, "", True))             # whole two-letter class
  return out


def plan_searches(shelves):
  """
  Prefix searches covering everything from the first shelf's start to the
  last shelf's end. Shelves are covered start-to-start, so gaps between one
  shelf's end and the next shelf's start are searched too.
  Returns sorted (term, wildcard).
  """
  spans_from = [s["start_parts"] for s in shelves]
  spans_to = [s["start_parts"] for s in shelves[1:]] + [shelves[-1]["end_parts"]]
  terms = set()
  for a, b in zip(spans_from, spans_to):
    a_letters, a_num = a[0], int(a[1])
    b_letters, b_num = b[0], int(b[1])
    if a_letters == b_letters:
      spans = [(a_letters, a_num, b_num)]
    else:
      spans = [(a_letters, a_num, None), (b_letters, 1, b_num)]
      terms.update(whole_class_searches(a_letters, b_letters))
    for letters, lo, hi in spans:
      for digits, wildcard in digit_cover(lo, hi):
        terms.add((letters, digits, wildcard))

  # Drop searches already covered by a broader one: QA76* is inside QA7*,
  # and anything starting with B is inside B*
  broad = {letters + digits for letters, digits, wildcard in terms if wildcard}
  kept = set()
  for letters, digits, wildcard in terms:
    term = letters + digits
    covered = any(term[:k] in broad for k in range(1, len(term)))
    if not covered:
      kept.add((term, wildcard))
  return sorted(kept)


# --------------------------------------------------------------------------
# 3. Running the searches
# --------------------------------------------------------------------------

YEARS = []        # years to split by, taken from the shelf list (see collect_all)


def split_search(term, wildcard):
  """
  Narrower searches that together cover everything one search matches.
  A search term can have several words (the catalog matches each word of
  the call number): "LD1780 1973 S*" means LD1780, then 1973, then a cutter
  starting with S.
  """
  words = term.split()
  last = words[-1]
  if not wildcard:
    if len(words) == 1 and YEARS:
      # LD1780 exactly -> one search per year: "LD1780 1913" ... "LD1780 2014"
      return [(f"{term} {year}", False) for year in YEARS]
    # LD1780 1973 (or QA76 exactly) -> by the first letter of the cutter
    return [(f"{term} {letter}", True) for letter in string.ascii_uppercase]
  if len(words) > 1:
    # LD1780 1973 S* -> LD1780 1973 S1* ... S9*;  S7* -> S70* ... S79*
    digits = "123456789" if last.isalpha() else string.digits
    return [(f"{term}{d}", True) for d in digits]
  if term.isalpha():
    # B* -> B1* ... B9*, BA* ... BZ*
    return [(term + c, True) for c in "123456789" + string.ascii_uppercase]
  if term.endswith("."):
    # QA76.* -> QA76.0* ... QA76.9*, and QA76.A* ... for cutters without a space
    return [(term + c, True) for c in string.digits + string.ascii_uppercase]
  if "." in term:
    # QA76.6* -> QA76.6 exactly, then QA76.60* ... QA76.69*
    return [(term, False)] + [(term + d, True) for d in string.digits]
  # QA76* -> QA76 exactly, its decimals (QA76.*), then QA760* ... QA769*
  return [(term, False), (term + ".", True)] + [(term + d, True) for d in string.digits]


def run_search(term, wildcard, facets, wanted, log, depth=0):
  """Run one search; if it has too many results to read, split it."""
  query = term + ("*" if wildcard else "")
  _, total = u.fetch_page(query, facets, 0, 1, raw=True)
  if total == 0:
    return []
  if total <= MAX_READABLE:
    results, _ = u.collect_results(query, facets, wanted, raw=True,
                                   max_results=MAX_READABLE)
    return results
  if depth >= 10:
    log.append(f"'{query}' still has {total} titles after splitting; only "
               f"{MAX_READABLE} could be read, so some may be missing")
    results, _ = u.collect_results(query, facets, wanted, raw=True,
                                   max_results=MAX_READABLE)
    return results

  print(f"    '{query}' has {total} titles, splitting it")
  if not wildcard and len(term.split()) == 1 and YEARS:
    log.append(f"'{query}' was split by year ({YEARS[0]}-{YEARS[-1]}); any "
               f"call numbers under it without a year are not covered")
  results = []
  for child_term, child_wild in split_search(term, wildcard):
    results += run_search(child_term, child_wild, facets, wanted, log, depth + 1)
  return results


def search_key(term, wildcard):
  return f"{','.join(LOCATIONS)}|{term}{'*' if wildcard else ''}"


def load_search_cache():
  """Searches finished in an earlier run, if that run was recent."""
  try:
    with open(SEARCH_CACHE_FILE, encoding="utf-8") as f:
      data = json.load(f)
    if time.time() - data.get("_saved", 0) > SEARCH_CACHE_MAX_AGE:
      return {}
    data.pop("_saved", None)
    return data
  except (OSError, ValueError):
    return {}


def save_json(path, data):
  """Write safely: a crash mid-write never leaves a broken file behind."""
  if path == SEARCH_CACHE_FILE:
    data = dict(data, _saved=time.time())
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f)
  os.replace(tmp, path)


def collect_all(shelves):
  global YEARS
  # Years that appear in the shelf list (e.g. dissertations: LD1780 1913 ...)
  found = [int(d) for s in shelves
           for letter, d in s["start_parts"][3] + s["end_parts"][3]
           if letter == YEAR]
  YEARS = list(range(min(found), max(found) + 1)) if found else []
  searches = plan_searches(shelves)
  if len(searches) > MAX_SEARCHES:
    sys.exit(f"The shelf list needs {len(searches)} searches (limit "
             f"{MAX_SEARCHES}). Split it into smaller lists, or raise "
             f"MAX_SEARCHES.")
  facets = u.build_facets(LOCATIONS)
  wanted = {c.upper() for c in LOCATIONS}
  print(f"Running {len(searches)} catalog searches...")

  cache = load_search_cache()
  reused = sum(1 for t, w in searches if search_key(t, w) in cache)
  if reused:
    print(f"  {reused} of them already done in an earlier run "
          f"(saved in {SEARCH_CACHE_FILE})")

  holdings, seen, log = [], set(), []
  for i, (term, wildcard) in enumerate(searches, 1):
    key = search_key(term, wildcard)
    if key not in cache:
      cache[key] = run_search(term, wildcard, facets, wanted, log)
      save_json(SEARCH_CACHE_FILE, cache)
      time.sleep(PAUSE_BETWEEN_SEARCHES)   # go easy on the shared catalog
    for r in cache[key]:
      key = (r["holding"].get("holdId"), r["call_number"])
      if key not in seen:
        seen.add(key)
        holdings.append(r)
    if i % 10 == 0 or i == len(searches):
      print(f"  {i}/{len(searches)} searches done, {len(holdings)} holdings so far")
  for line in log:
    print(f"  WARNING: {line}")
  return holdings, log


# --------------------------------------------------------------------------
# 4. Shelves and item counts
# --------------------------------------------------------------------------

def assign_to_shelves(holdings, shelves):
  """
  Like reading shelf labels: a book is on the last shelf whose start comes at
  or before it. So anything between one shelf's end label and the next
  shelf's start label counts toward the earlier shelf. Only the last shelf's
  end is used as a cutoff (at the detail it's written: "QR1.A45" includes
  "QR1.A452").
  Returns (outside, in_gaps): holdings beyond the list, and how many fell
  between a shelf's end and the next start.
  """
  for s in shelves:
    s["holdings"] = []
  ordered = sorted(shelves, key=lambda s: lc_key(s["start_parts"]))
  first, last = ordered[0], ordered[-1]
  outside, in_gaps = [], 0
  for h in holdings:
    parts = parse_lc(h["call_number"])
    if (not parts or lc_key(parts) < lc_key(first["start_parts"])
        or not in_lc_range(h["call_number"], last["start_parts"],
                             last["end_parts"])
        and lc_key(parts) > lc_key(last["start_parts"])):
      outside.append(h)
      continue
    shelf = [s for s in ordered if lc_key(s["start_parts"]) <= lc_key(parts)][-1]
    shelf["holdings"].append(h)
    if not in_lc_range(h["call_number"], shelf["start_parts"], shelf["end_parts"]):
      in_gaps += 1
  return outside, in_gaps


def load_cache():
  try:
    with open(CACHE_FILE, encoding="utf-8") as f:
      return json.load(f)
  except (OSError, ValueError):
    return {}


def save_cache(cache):
  save_json(CACHE_FILE, cache)


def add_item_counts(shelves):
  cache = load_cache()
  todo = [h for s in shelves for h in s["holdings"]
          if str(h["holding"].get("holdId")) not in cache]
  done_before = sum(len(s["holdings"]) for s in shelves) - len(todo)
  if done_before:
    print(f"\n{done_before} item counts reused from {CACHE_FILE}")
  if todo:
    minutes = len(todo) * 1.5 / 60
    print(f"Counting items for {len(todo)} holdings (about {minutes:.0f} min)...")

  failures = 0
  for i, h in enumerate(todo, 1):
    hold_id = str(h["holding"].get("holdId"))
    try:
      counts = u.fetch_item_counts(h["doc"], h["holding"])
      cache[hold_id] = {"items": counts["items"],
                        "volumes": len(counts["volumes"]),
                        "call_number": h["call_number"]}
    except (requests.exceptions.RequestException, LookupError, ValueError) as e:
      failures += 1
      if failures <= 3:
        print(f"  Item count failed for {h['call_number']}: {e}")
    if i % 10 == 0 or i == len(todo):
      save_cache(cache)
    if i % 50 == 0 or i == len(todo):
      print(f"  {i}/{len(todo)} done ({100 * i // len(todo)}%)")
    time.sleep(u.PAUSE_BETWEEN_ITEM_REQUESTS)
  save_cache(cache)
  if failures:
    print(f"  {failures} item count(s) failed. Run the script again to retry "
          f"just those.")

  for s in shelves:
    for h in s["holdings"]:
      entry = cache.get(str(h["holding"].get("holdId")))
      h["items"] = entry["items"] if entry else None
      h["volume_count"] = entry.get("volumes") if entry else None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def record_id(h):
  return (h["doc"].get("pnx", {}).get("control", {}).get("recordid")
          or [h["title"]])[0]


def write_details(shelves, count_items):
  """One row per holding, in shelf order: the raw data behind the counts."""
  with open(DETAIL_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    header = ["shelf", "call_number", "title"]
    if count_items:
      header += ["items", "volumes"]
    writer.writerow(header + ["record_id"])
    for s in shelves:
      for h in sorted(s["holdings"], key=lambda h: lc_sort_key(h["call_number"])):
        row = [s["shelf"], h["call_number"], h["title"]]
        if count_items:
          row += ["" if h.get("items") is None else h["items"],
                  "" if h.get("volume_count") is None else h["volume_count"]]
        writer.writerow(row + [record_id(h)])
  print(f"Each holding's call number and count saved to {DETAIL_FILE}")


def write_output(shelves, count_items):
  rows = []
  has_length = all(s["length"] for s in shelves)
  for s in shelves:
    records = {record_id(h) for h in s["holdings"]}
    row = {"shelf": s["shelf"], "start": s["start"], "end": s["end"],
           "titles": len(records)}
    if count_items:
      counted = [h["items"] for h in s["holdings"] if h.get("items") is not None]
      row["items"] = sum(counted)
      row["items_missing"] = len(s["holdings"]) - len(counted)
    if has_length:
      measure = row["items"] if count_items else row["titles"]
      row["length"] = s["length"]
      row["per_unit_length"] = round(measure / s["length"], 2)
    rows.append(row)

  with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

  w = max([32] + [len(r["start"]) + len(r["end"]) + 5 for r in rows])
  print(f"\n{'Shelf':<10}{'Range':<{w}}{'Titles':>8}" +
        (f"{'Items':>8}" if count_items else ""))
  for r in rows:
    line = f"{r['shelf']:<10}{(r['start'] + ' – ' + r['end']):<{w}}{r['titles']:>8}"
    if count_items:
      line += f"{r['items']:>8}"
      if r["items_missing"]:
        line += f"   ({r['items_missing']} holding(s) not counted)"
    print(line)
  total_titles = sum(r["titles"] for r in rows)
  print(f"\nTotal: {total_titles} titles" +
        (f", {sum(r['items'] for r in rows)} items" if count_items else ""))
  print(f"Saved to {OUTPUT_FILE}")
  return rows


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------

BAR = "#2a78d6"            # single series: one hue, validated on the surface
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"


def make_chart(rows, count_items):
  """
  Bar chart of the count per shelf; if shelf lengths were given, a second
  chart below of count per unit of length (the true density). Two separate
  charts rather than one with two y-axes, since they're different scales.
  """
  try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
  except ImportError:
    print("Chart skipped: install matplotlib (pip install matplotlib) to get "
          f"{CHART_FILE}.")
    return

  measure = "items" if count_items else "titles"
  panels = [(measure, f"Physical {measure} per shelf" if count_items
             else "Titles per shelf", measure.capitalize())]
  if rows and "per_unit_length" in rows[0]:
    panels.append(("per_unit_length", f"{measure.capitalize()} per unit of "
                   f"shelf length", f"{measure.capitalize()} per unit length"))

  names = [str(r["shelf"]) for r in rows]
  width = min(16, max(7, 0.35 * len(rows) + 3))
  fig, axes = plt.subplots(len(panels), 1, figsize=(width, 3.6 * len(panels)),
                           facecolor=SURFACE, squeeze=False)
  for ax, (key, title, ylabel) in zip(axes[:, 0], panels):
    values = [r[key] for r in rows]
    ax.set_facecolor(SURFACE)
    ax.bar(range(len(rows)), values, width=0.82, color=BAR, zorder=2)
    ax.set_title(title, loc="left", color=TEXT, fontsize=12, pad=10)
    ax.set_ylabel(ylabel, color=TEXT_SECONDARY, fontsize=9)
    ax.set_xlabel("Shelf", color=TEXT_SECONDARY, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
      ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8, length=0)
    step = max(1, len(rows) // 40)        # keep shelf labels readable
    ax.set_xticks(range(0, len(rows), step))
    long_names = max(len(n) for n in names) > 3
    ax.set_xticklabels(names[::step],
                       rotation=90 if long_names and len(rows) > 12 else 0)
    ax.set_xlim(-0.6, len(rows) - 0.4)
    # Label only the busiest shelf; anchor the text away from the chart edges
    if values and max(values) > 0:
      i = values.index(max(values))
      position = i / max(1, len(rows) - 1)
      if position < 0.25:
        ha, dx = "left", -6
      elif position > 0.75:
        ha, dx = "right", 6
      else:
        ha, dx = "center", 0
      ax.annotate(f"{values[i]:g}  (shelf {names[i]}: {rows[i]['start']}–"
                  f"{rows[i]['end']})", (i, values[i]), textcoords="offset points",
                  xytext=(dx, 4), ha=ha, fontsize=8, color=TEXT)
      ax.set_ylim(0, max(values) * 1.15)
  fig.tight_layout()
  fig.savefig(CHART_FILE, dpi=150, facecolor=SURFACE)
  plt.close(fig)
  print(f"Chart saved to {CHART_FILE}")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("shelves", nargs="?", default="shelves.csv",
                      help="CSV of shelves with columns shelf,start,end")
  parser.add_argument("--titles-only", action="store_true",
                      help="skip item counts (much faster)")
  parser.add_argument("--fresh", action="store_true",
                      help="ignore results saved by earlier runs and start over")
  args = parser.parse_args()
  if args.fresh:
    for path in (SEARCH_CACHE_FILE, CACHE_FILE):
      if os.path.exists(path):
        os.remove(path)
    print("Starting fresh: earlier saved results removed.")

  print(f"shelf_density.py, using uf_callnumber_search.py version "
        f"{u.SCRIPT_VERSION}")
  if "raw" not in inspect.signature(u.fetch_page).parameters:
    sys.exit("uf_callnumber_search.py is an older copy. Replace it with the "
             "version sent alongside this script (2026-10-17e or later).")
  shelves = read_shelves(args.shelves)
  print(f"{len(shelves)} shelves, {shelves[0]['start']} to {shelves[-1]['end']}")

  try:
    holdings, _ = collect_all(shelves)
  except requests.exceptions.RequestException as e:
    sys.exit(f"Error connecting to the catalog: {e}")

  outside, in_gaps = assign_to_shelves(holdings, shelves)
  print(f"{len(holdings) - len(outside)} holdings fall on your shelves "
        f"({len(outside)} found by the searches were before the first shelf "
        f"or after the last and were left out)")
  if in_gaps:
    print(f"  {in_gaps} of them sit between one shelf's end label and the next "
          f"shelf's start label; they count toward the earlier shelf")

  if not args.titles_only:
    add_item_counts(shelves)
  rows = write_output(shelves, count_items=not args.titles_only)
  write_details(shelves, count_items=not args.titles_only)
  make_chart(rows, count_items=not args.titles_only)


if __name__ == "__main__":
  main()