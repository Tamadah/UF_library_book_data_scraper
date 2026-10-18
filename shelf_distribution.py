"""
Make the distribution of items along each shelf, and use it to estimate where
any call number sits on its shelf.

Reads  holdings_detail.csv  (from shelf_density.py: every journal in shelf
order with its item count) and  shelves.csv  (your shelf list), and writes:

  shelves.csv may have a  bins  column: how many bins to cut that shelf into.
  Shelves without one use --bins (default 20).

1. shelf_distribution.csv  (one row per journal, as before)
     shelf, position, call_number, items, probability, cumulative

2. shelf_density_bins.csv  (no journals or call numbers: just the curve)
     shelf, bin, range_from, range_to, probability, cumulative_from, cumulative_to

   Each shelf's call-number range, from its start label to its end label,
   is treated as a straight line from 0 to 1 and cut into equal bins.
     range_from, range_to       where the bin sits on that line
     probability                share of the shelf's items in the bin
     cumulative_from/_to        share of the shelf filled before/after the bin

   A call number at point x on the line, inside a bin, is estimated to sit at
     cumulative_from + probability * (x - range_from) / (range_to - range_from)
   of the way along the shelf. Busy bins stretch out; empty bins take no room.

Run:
  python shelf_distribution.py                       make both files
  python shelf_distribution.py --bins 50             finer bins (default 20)
  python shelf_distribution.py --locate "QK1 .J6"    estimate a position
                                                     (uses shelf_density_bins.csv)

Standalone: no other scripts or libraries needed.
"""
import argparse
import csv
import re
import sys
from decimal import Decimal, getcontext

getcontext().prec = 50

DETAIL_FILE = "holdings_detail.csv"
SHELVES_FILE = "shelves.csv"
JOURNALS_OUT = "shelf_distribution.csv"
BINS_OUT = "shelf_density_bins.csv"
DEFAULT_BINS = 20


# --------------------------------------------------------------------------
# Call numbers as points on a line
# --------------------------------------------------------------------------

LC_PATTERN = re.compile(r"^\s*([A-Z]{1,3})\s*(\d+)(?:\.(\d+))?(.*)$")
CUTTER_PATTERN = re.compile(r"^\s*\.?\s*([A-Z])(\d+)")
YEAR_PATTERN = re.compile(r"^\s*(\d{4})(?!\d)[A-Z]?")


def parse_lc(call_number):
  """
  'QA76.65 .C662 2009' -> ('QA', '76', '65', [('C', '662'), ('#', '2009')])
  'LD1780 1973 .S714'  -> ('LD', '1780', '', [('#', '1973'), ('S', '714')])
  After the class number come cutters and 4-digit years, in order.
  Returns None if it isn't an LC call number.
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
      parts.append(("#", y.group(1)))
      rest = rest[y.end():]
      continue
    break
  return letters, whole, decimal, parts


def lc_value(call_number):
  """
  A number that grows in LC shelf order, so call numbers can be compared
  on a straight line. The call number is written out as fixed-width digit
  fields, most significant first, and read as one big number:
    class letters  2 digits per letter, 3 letters   Q < QA < QB
    class number   4 digits                         9 < 76 < 641
    decimal        4 digits                         76.6 < 76.65 < 76.9
    then up to 4 parts of 9 digits each:
      kind         0 = none, 1 = year, 2 = cutter   (a year before a cutter)
      letter       2 digits (00 for a year)
      digits       6 digits: a year as a number, a cutter as a decimal
                   (D25 < D251 < D3)
  Fixed widths keep shelf order exact; the digits are spread evenly, so
  points between two call numbers interpolate in a straight line.
  Returns None if it isn't an LC call number.
  """
  parsed = parse_lc(call_number)
  if not parsed:
    return None
  letters, whole, decimal, parts = parsed
  text = "".join(f"{ord(letters[i]) - 64:02d}" if i < len(letters) else "00"
                 for i in range(3))
  text += whole[-4:].zfill(4) + decimal[:4].ljust(4, "0")
  for i in range(4):
    if i >= len(parts):
      text += "0" + "00" + "000000"
      continue
    letter, digits = parts[i]
    if letter == "#":
      text += "1" + "00" + digits.zfill(6)
    else:
      text += "2" + f"{ord(letter) - 64:02d}" + digits[:6].ljust(6, "0")
  return Decimal(int(text))


def line_position(call_number, start_value, end_value):
  """Where a call number falls on the shelf's 0..1 line (clamped to it)."""
  v = lc_value(call_number)
  if v is None or end_value <= start_value:
    return None if v is None else 0.0
  x = float((v - start_value) / (end_value - start_value))
  return min(1.0, max(0.0, x))


# --------------------------------------------------------------------------
# Reading the inputs
# --------------------------------------------------------------------------

def read_csv(path, what):
  try:
    with open(path, newline="", encoding="utf-8-sig") as f:
      return list(csv.DictReader(f))
  except FileNotFoundError:
    sys.exit(f"{path} not found. {what}")


def read_shelves(path):
  shelves = {}
  for row in read_csv(path, "It's your shelf list (shelf,start,end[,length])."):
    row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    if not row.get("shelf"):
      continue
    start, end = lc_value(row["start"]), lc_value(row["end"])
    if start is None or end is None:
      sys.exit(f"{path}: shelf {row['shelf']} has a start or end that isn't an "
               f"LC call number")
    length = float(row["length"]) if row.get("length") else None
    bins = None
    if row.get("bins"):
      try:
        bins = int(float(row["bins"]))
      except ValueError:
        bins = 0
      if bins < 1:
        sys.exit(f"{path}: shelf {row['shelf']} has bins '{row['bins']}'; "
                 f"it must be a whole number of 1 or more")
    shelves[row["shelf"]] = {"start": row["start"], "end": row["end"],
                             "start_value": start, "end_value": end,
                             "length": length, "bins": bins}
  return shelves


def read_journals(path):
  """Journals per shelf, in the file's order (already shelf order)."""
  journals, missing = {}, 0
  for row in read_csv(path, "Run shelf_density.py first; it creates it."):
    text = (row.get("items") or "").strip()
    if text:
      items = int(float(text))
    else:
      items = 1                 # count unknown: treat as one item
      missing += 1
    journals.setdefault(row["shelf"], []).append((row["call_number"], items))
  return journals, missing


# --------------------------------------------------------------------------
# 1. Per-journal distribution (unchanged)
# --------------------------------------------------------------------------

def write_journal_distribution(journals, path):
  with open(path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["shelf", "position", "call_number", "items",
                     "probability", "cumulative"])
    for shelf, rows in journals.items():
      total = sum(items for _, items in rows)
      running = 0
      for position, (call_number, items) in enumerate(rows, 1):
        running += items
        writer.writerow([shelf, position, call_number, items,
                         round(items / total, 6) if total else 0,
                         round(running / total, 6) if total else 0])


# --------------------------------------------------------------------------
# 2. Binned density along each shelf's call-number range
# --------------------------------------------------------------------------

def build_bins(shelf, rows, n_bins):
  """Share of the shelf's items in each equal slice of its call-number range."""
  counts = [0] * n_bins
  for call_number, items in rows:
    x = line_position(call_number, shelf["start_value"], shelf["end_value"])
    if x is None:
      continue                  # not an LC call number: can't place it
    counts[min(n_bins - 1, int(x * n_bins))] += items
  total = sum(counts)
  bins, running = [], 0.0
  for b, count in enumerate(counts):
    p = count / total if total else 1 / n_bins   # no data: plain straight line
    bins.append({"bin": b + 1,
                 "range_from": b / n_bins, "range_to": (b + 1) / n_bins,
                 "probability": p,
                 "cumulative_from": running, "cumulative_to": running + p})
    running += p
  return bins


def write_bins(shelves, journals, n_bins, path):
  with open(path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["shelf", "bin", "range_from", "range_to", "probability",
                     "cumulative_from", "cumulative_to"])
    for name, shelf in shelves.items():
      # The shelf's own bins column if it has one, otherwise --bins
      for b in build_bins(shelf, journals.get(name, []), shelf["bins"] or n_bins):
        writer.writerow([name, b["bin"], round(b["range_from"], 6),
                         round(b["range_to"], 6), round(b["probability"], 6),
                         round(b["cumulative_from"], 6),
                         round(b["cumulative_to"], 6)])


# --------------------------------------------------------------------------
# Estimating a position from the bins file
# --------------------------------------------------------------------------

def read_bins(path):
  bins = {}
  for row in read_csv(path, "Run  python shelf_distribution.py  first."):
    bins.setdefault(row["shelf"], []).append(
        {k: float(row[k]) for k in ("range_from", "range_to", "probability",
                                    "cumulative_from")})
  return bins


def find_shelf(call_number, shelves):
  """The last shelf whose start label is at or before the call number."""
  v = lc_value(call_number)
  if v is None:
    return None, None
  candidates = [(s["start_value"], name) for name, s in shelves.items()
                if s["start_value"] <= v]
  if not candidates:
    return None, v
  return max(candidates)[1], v


def locate(call_number, shelves, bins):
  name, v = find_shelf(call_number, shelves)
  if v is None:
    return f"'{call_number}' isn't an LC call number"
  if name is None:
    return f"'{call_number}' comes before your first shelf"
  shelf = shelves[name]
  x = line_position(call_number, shelf["start_value"], shelf["end_value"])
  for b in bins.get(name, []):
    if b["range_from"] <= x <= b["range_to"]:
      width = b["range_to"] - b["range_from"]
      along = b["cumulative_from"] + b["probability"] * (x - b["range_from"]) / width
      break
  else:
    along = x                    # no bins for this shelf: straight line
  along = min(1.0, max(0.0, along))
  text = (f"{call_number}\n  Shelf {name} ({shelf['start']} – {shelf['end']})\n"
          f"  {100 * x:.0f}% of the way through the shelf's call-number range\n"
          f"  -> about {100 * along:.0f}% along the shelf")
  if shelf["length"]:
    text += f" ({along * shelf['length']:.1f} of {shelf['length']:g} from the start)"
  return text


# --------------------------------------------------------------------------

def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("--details", default=DETAIL_FILE)
  parser.add_argument("--shelves", default=SHELVES_FILE)
  parser.add_argument("--bins", type=int, default=DEFAULT_BINS,
                      help="bins for shelves without a bins column value "
                           f"(default {DEFAULT_BINS})")
  parser.add_argument("--locate", nargs="+", metavar="CALL_NUMBER",
                      help="estimate where these call numbers sit, using "
                           f"{BINS_OUT}")
  args = parser.parse_args()

  shelves = read_shelves(args.shelves)

  if args.locate:
    bins = read_bins(BINS_OUT)
    for call_number in args.locate:
      print(locate(call_number, shelves, bins))
    return

  journals, missing = read_journals(args.details)
  write_journal_distribution(journals, JOURNALS_OUT)
  write_bins(shelves, journals, args.bins, BINS_OUT)
  print(f"{len(shelves)} shelves, {sum(len(j) for j in journals.values())} "
        f"journals -> {JOURNALS_OUT}")
  own = [s for s in shelves.values() if s["bins"]]
  if own and len(own) == len(shelves):
    print(f"Bins per shelf from {args.shelves} -> {BINS_OUT}")
  elif own:
    print(f"Bins per shelf from {args.shelves} ({len(own)} shelves), "
          f"{args.bins} for the other {len(shelves) - len(own)} -> {BINS_OUT}")
  else:
    print(f"{args.bins} bins per shelf -> {BINS_OUT}")
  if missing:
    print(f"Note: {missing} journal(s) had no item count and were counted as 1 "
          f"item. Rerun shelf_density.py to fill them in.")


if __name__ == "__main__":
  main()