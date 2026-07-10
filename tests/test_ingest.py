"""Tests for HTML/PDF receipt ingestion against the real sample receipt."""
from pathlib import Path

from costco_archiver.ingest import receipt_from_html, receipt_from_pdf

# The 22 line items from the sample receipt (W TAMPA #1262, 07/06/2026).
ITEMS = [
    ("E", "553499", "DRIED PLUMS", "9.99", "N"),
    ("E", "1856165", "APRICOTS", "15.79", "N"),
    ("E", "1531775", "CIL/LIME CHK", "44.97", "N"),
    ("", "135576", "ZIPFIZZ 30CT", "29.99", "N"),
    ("E", "1495250", "KSCOCOWTR 1L", "19.99", "N"),
    ("E", "7017", "3LB ORG ENVY", "6.49", "N"),
    ("E", "2619", "ORG BANANAS", "2.19", "N"),
    ("E", "1048072", "GREEK YOGURT", "14.78", "N"),
    ("E", "42940", "STUFFED PEP", "38.77", "Y"),
    ("E", "1652577", "KS UNSLT NUT", "16.59", "N"),
    ("E", "931484", "KS WATER GAL", "5.49", "N"),
    ("E", "647465", "AVOCADOS", "12.98", "N"),
    ("", "1990467", "FARMLAND CHN", "23.89", "Y"),
    ("E", "1508784", "ROASTED VEG", "32.98", "N"),
    ("E", "2049097", "HONEST COW", "7.49", "N"),
    ("E", "411155", "GRAN ONION", "5.99", "N"),
    ("", "617686", "SOFTSOAP", "11.99", "Y"),
    ("E", "1656653", "FROZEN BERRY", "23.98", "N"),
    ("E", "61576", "SWEET POTATO", "3.99", "N"),
    ("E", "1364698", "KS ORG 2% MK", "11.79", "N"),
    ("", "887498", "KSAKFISHOIL", "21.99", "N"),
    ("E", "782796", "***KSWTR40PK", "3.99", "N"),
]


def _build_html() -> str:
    """Reproduce the sample's MUI receipt structure (real class names)."""
    rows = "".join(
        f'<tr class="MuiTableRow-root css-ufft4h">'
        f'<td class="MuiTableCell-root css-uo1frn">{flag}</td>'
        f'<td class="MuiTableCell-root css-tedx13">{num}</td>'
        f'<td class="MuiTableCell-root css-u9y9s5">{desc}</td>'
        f'<td class="MuiTableCell-root css-1879r0q">{amt} {tf}</td></tr>'
        for flag, num, desc, amt, tf in ITEMS
    )
    return f"""<div id="dataToPrint"><div class="wrapper">
      <div class="MuiTypography-root MuiTypography-bodyCopy header css-px5qu0">W TAMPA #1262</div>
      <div class="MuiTypography-root MuiTypography-bodyCopy address css-g1tw1">8712 W LINEBAUGH AVE</div>
      <div class="MuiTypography-root MuiTypography-bodyCopy address1 css-g1tw1">TAMPA, FL 33625</div>
      <div class="barcode"><div class="MuiBox-root css-11s8ayx">
        <img src="data:image/png;base64,SHORT" alt="barcode"></div>
        <div class="MuiBox-root css-11s8ayx">21126200602132607061652</div></div>
      <table><thead><tr><th colspan="4">Member 111866191307</th></tr></thead>
      <tbody>
      {rows}
      <tr><td colspan="2"></td><td>SUBTOTAL</td><td>366.10</td></tr>
      <tr><td colspan="2"></td><td>TAX</td><td><span> 5.60</span></td></tr>
      <tr><td></td><td>****</td><td class="upperCase">Total</td><td><span>371.70</span></td></tr>
      <tr><td colspan="3">XXXXXXXXXXXXX6769</td><td>CHIP read</td></tr>
      <tr><td colspan="3">AMOUNT: $371.70</td><td></td></tr>
      <tr><td colspan="5"><span class="date">07/06/2026</span><span class="time">16:52</span>
        <span>1262</span><span>6</span><span>213</span><span>607</span></td></tr>
      <tr><td></td><td>(A) A</td><td>5.60</td></tr>
      <tr><td></td><td>TOTAL TAX</td><td>5.60</td></tr>
      <tr><td colspan="5">TOTAL NUMBER OF ITEMS SOLD = 29</td></tr>
      </tbody></table></div></div>"""


def test_html():
    r = receipt_from_html(_build_html())
    assert r["transactionBarcode"] == "21126200602132607061652", r["transactionBarcode"]
    assert r["warehouseName"] == "W TAMPA #1262"
    assert r["warehouseNumber"] == "1262"
    assert r["transactionDate"] == "2026-07-06"
    assert r["transactionDateTime"] == "2026-07-06T16:52:00"
    assert r["member"] == "111866191307"
    assert r["subTotal"] == 366.10, r["subTotal"]
    assert r["taxes"] == 5.60, r["taxes"]
    assert r["total"] == 371.70, r["total"]
    assert r["totalItemCount"] == 29, r["totalItemCount"]
    assert len(r["itemArray"]) == 22, len(r["itemArray"])
    s = round(sum(i["amount"] for i in r["itemArray"]), 2)
    assert s == 366.10, s
    by_num = {i["itemNumber"]: i for i in r["itemArray"]}
    assert by_num["42940"]["taxFlag"] == "Y"
    assert by_num["553499"]["itemDescription01"] == "DRIED PLUMS"
    assert by_num["1495250"]["amount"] == 19.99
    print("HTML ingest OK — 22 items, totals reconcile")


def test_pdf():
    p = Path(__file__).resolve().parent.parent / "myaccount.pdf"
    if not p.exists():
        print("(myaccount.pdf not present, skipping PDF test)")
        return
    r = receipt_from_pdf(p)
    s = round(sum(i["amount"] for i in r["itemArray"]), 2)
    assert len(r["itemArray"]) == 22, len(r["itemArray"])
    assert s == r["subTotal"] == 366.10, (s, r["subTotal"])
    assert r["taxes"] == 5.60 and r["total"] == 371.70
    print("PDF ingest OK — 22 items, totals reconcile")


if __name__ == "__main__":
    test_html()
    test_pdf()
    print("\nALL INGEST TESTS PASSED")
