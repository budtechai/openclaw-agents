#!/usr/bin/env python3
import argparse
import base64
import email.message
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from dateutil import tz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

@dataclass
class Listing:
    source: str
    subject: str
    received: str
    address: Optional[str] = None
    zip: Optional[str] = None
    price: Optional[int] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    url: Optional[str] = None
    raw: Optional[str] = None

@dataclass
class CashflowResult:
    loan_amount: float
    pi_monthly: float
    tax_monthly: float
    insurance_monthly: float
    reserves_monthly: float
    noi_monthly: float
    cashflow_monthly: float
    cap_rate: float
    dscr: float
    coc: float


def money_int(s: str) -> Optional[int]:
    if not s:
        return None
    s = s.replace(",", "")
    m = re.search(r"\$\s*([0-9]+)", s)
    if not m:
        return None
    return int(m.group(1))


def parse_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def extract_text_from_payload(payload: dict) -> Tuple[str, str]:
    """Return (text, html) best-effort."""
    text_parts = []
    html_parts = []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
            if mime == "text/plain":
                text_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return ("\n".join(text_parts).strip(), "\n".join(html_parts).strip())


def extract_urls(text: str) -> List[str]:
    # coarse
    urls = re.findall(r"https?://[^\s)\]]+", text or "")
    # de-dupe while preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def best_effort_parse_listing(source: str, subject: str, received: str, text: str, html: str) -> List[Listing]:
    content = text
    if not content and html:
        soup = BeautifulSoup(html, "lxml")
        content = soup.get_text("\n")
    content = content or ""

    urls = extract_urls(html or "") + extract_urls(text or "")
    urls = [u for u in urls if any(k in u for k in ("zillow.com", "redfin.com"))]

    # Try to find single obvious price/beds/baths/zip in the whole email.
    price = None
    beds = None
    baths = None
    zip_code = None

    m_price = re.search(r"\$\s*([0-9][0-9,]{2,})", content)
    if m_price:
        price = int(m_price.group(1).replace(",", ""))

    m_beds = re.search(r"\b([0-9]+(?:\.[0-9])?)\s*(?:bd|bed|beds)\b", content, flags=re.IGNORECASE)
    if m_beds:
        beds = parse_float(m_beds.group(1))

    m_baths = re.search(r"\b([0-9]+(?:\.[0-9])?)\s*(?:ba|bath|baths)\b", content, flags=re.IGNORECASE)
    if m_baths:
        baths = parse_float(m_baths.group(1))

    m_zip = re.search(r"\b(0[0-9]{4})\b", content)
    if m_zip:
        zip_code = m_zip.group(1)

    # Address is hard; try common patterns like "123 Main St" if present.
    m_addr = re.search(r"\b([0-9]{1,6}\s+[A-Za-z0-9 .'-]+\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Pl|Place|Ct|Court|Ter|Terrace|Way))\b",
                      content)
    addr = m_addr.group(1).strip() if m_addr else None

    listings = []
    if urls:
        for u in urls[:10]:
            listings.append(Listing(source=source, subject=subject, received=received, address=addr, zip=zip_code, price=price, beds=beds, baths=baths, url=u, raw=None))
    else:
        listings.append(Listing(source=source, subject=subject, received=received, address=addr, zip=zip_code, price=price, beds=beds, baths=baths, url=None, raw=None))

    return listings


def amortized_pi(loan: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12.0
    n = years * 12
    if r == 0:
        return loan / n
    return loan * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def run_cashflow(price: int, rent_monthly: float, down: float, annual_rate: float, years: int,
                 tax_rate: float, insurance_monthly: float, reserves_pct: float,
                 closing_costs: float = 0.0) -> CashflowResult:
    loan = max(price - down, 0)
    pi = amortized_pi(loan, annual_rate, years)
    tax_m = (price * tax_rate) / 12.0
    reserves_m = rent_monthly * reserves_pct
    noi_m = rent_monthly - tax_m - insurance_monthly - reserves_m
    cashflow_m = noi_m - pi
    cap = (noi_m * 12.0) / price if price > 0 else 0.0
    dscr = (noi_m / pi) if pi > 0 else 0.0
    invested = down + closing_costs
    coc = ((cashflow_m * 12.0) / invested) if invested > 0 else 0.0
    return CashflowResult(
        loan_amount=loan,
        pi_monthly=pi,
        tax_monthly=tax_m,
        insurance_monthly=insurance_monthly,
        reserves_monthly=reserves_m,
        noi_monthly=noi_m,
        cashflow_monthly=cashflow_m,
        cap_rate=cap,
        dscr=dscr,
        coc=coc,
    )


def gmail_auth(creds_path: str, token_path: str):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def gmail_search(service, user_id: str, query: str, max_results: int = 50) -> List[str]:
    msg_ids = []
    resp = service.users().messages().list(userId=user_id, q=query, maxResults=max_results).execute()
    for m in resp.get("messages", []) or []:
        msg_ids.append(m["id"])
    return msg_ids


def gmail_get_message(service, user_id: str, msg_id: str) -> dict:
    return service.users().messages().get(userId=user_id, id=msg_id, format="full").execute()


def header_map(headers: List[dict]) -> dict:
    out = {}
    for h in headers or []:
        out[h.get("name", "").lower()] = h.get("value", "")
    return out


def to_dt_local(ms_epoch: int, tzname: str) -> datetime:
    z = tz.gettz(tzname)
    return datetime.fromtimestamp(ms_epoch / 1000.0, tz=z)


def format_brief(all_msgs: int, listings: List[Tuple[Listing, Optional[CashflowResult]]], criteria: dict) -> str:
    lines = []
    lines.append(f"Morning brief — {datetime.now(tz=tz.gettz(criteria['tz'])).strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append(f"Overnight alert emails scanned: {all_msgs}")
    lines.append(f"Matches: {len(listings)}")
    lines.append("")

    if not listings:
        lines.append("No matches against criteria.")
        return "\n".join(lines)

    lines.append("Matches (best-effort parse):")
    lines.append("")
    for i, (li, cf) in enumerate(listings[:15], 1):
        lines.append(f"{i}. {li.address or '[address unknown]'} {li.zip or ''}".strip())
        lines.append(f"   Source: {li.source} | Price: {('${:,}'.format(li.price) if li.price else 'n/a')} | Beds: {li.beds if li.beds is not None else 'n/a'} | Baths: {li.baths if li.baths is not None else 'n/a'}")
        if li.url:
            lines.append(f"   Link: {li.url}")
        if cf:
            lines.append(f"   Cashflow: {cf.cashflow_monthly:,.0f}/mo | DSCR: {cf.dscr:.2f} | Cap: {cf.cap_rate*100:.2f}% | CoC: {cf.coc*100:.2f}%")
            lines.append(f"   P&I: {cf.pi_monthly:,.0f} | Tax: {cf.tax_monthly:,.0f} | Ins: {cf.insurance_monthly:,.0f} | Reserves: {cf.reserves_monthly:,.0f}")
        lines.append("")

    lines.append("Notes:")
    lines.append("- Parsing is best-effort until we train the parser on your actual Zillow/Redfin templates.")
    lines.append("- Cashflow uses defaults unless the email contains clear rent/price/beds/baths/zip.")
    return "\n".join(lines).strip() + "\n"


def create_and_send_email(service, from_user: str, to_addr: str, subject: str, body_text: str):
    msg = email.message.EmailMessage()
    msg["To"] = to_addr
    msg["From"] = to_addr if from_user == "me" else from_user
    msg["Subject"] = subject
    msg.set_content(body_text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId=from_user, body={"raw": raw}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gmail", default="me", help="Gmail userId (default: me)")
    ap.add_argument("--to", required=True, help="Where to email the brief")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--lookback-hours", type=int, default=24)
    ap.add_argument("--max-emails", type=int, default=50)

    # Criteria
    ap.add_argument("--zip", action="append", default=["07306","07304","07305","07105","07047"])
    ap.add_argument("--min-beds", type=float, default=3)
    ap.add_argument("--max-beds", type=float, default=4)
    ap.add_argument("--min-baths", type=float, default=2)
    ap.add_argument("--max-price", type=int, default=600000)

    # Underwriting defaults
    ap.add_argument("--down", type=float, default=200000)
    ap.add_argument("--rate", type=float, default=0.07)
    ap.add_argument("--term-years", type=int, default=30)
    ap.add_argument("--tax-rate", type=float, default=0.0192)
    ap.add_argument("--insurance", type=float, default=120)
    ap.add_argument("--reserves-pct", type=float, default=0.15)
    ap.add_argument("--default-rent", type=float, default=3500)

    ap.add_argument("--creds", default=os.path.expanduser("~/.openclaw/workspace/morning_brief/credentials.json"))
    ap.add_argument("--token", default=os.path.expanduser("~/.openclaw/workspace/morning_brief/token.json"))
    ap.add_argument("--out", default=os.path.expanduser("~/.openclaw/workspace/morning_brief/latest_brief.md"))

    args = ap.parse_args()

    if not os.path.exists(args.creds):
        raise SystemExit(
            f"Missing OAuth client file at {args.creds}.\n"
            "Create a Google Cloud OAuth Client (Desktop app) and download credentials.json to that path."
        )

    svc = gmail_auth(args.creds, args.token)

    # Search query (simple + reliable): last N hours and (Zillow OR Redfin)
    # Gmail query supports newer_than:Xd / Xh
    q = f"newer_than:{args.lookback_hours}h (zillow OR redfin)"
    ids = gmail_search(svc, args.gmail, q, max_results=args.max_emails)

    listings: List[Listing] = []
    for mid in ids:
        msg = gmail_get_message(svc, args.gmail, mid)
        hm = header_map(msg.get("payload", {}).get("headers", []))
        subject = hm.get("subject", "(no subject)")
        frm = hm.get("from", "")
        internal_ms = int(msg.get("internalDate", "0"))
        received_local = to_dt_local(internal_ms, args.tz).strftime("%Y-%m-%d %H:%M %Z")

        text, html = extract_text_from_payload(msg.get("payload", {}))
        src = "Zillow" if "zillow" in (frm + subject).lower() else ("Redfin" if "redfin" in (frm + subject).lower() else "Other")
        listings.extend(best_effort_parse_listing(src, subject, received_local, text, html))

    # Filter + cashflow
    matches: List[Tuple[Listing, Optional[CashflowResult]]] = []
    for li in listings:
        if li.price is None or li.beds is None or li.baths is None or li.zip is None:
            # Can't reliably filter; skip for now.
            continue
        if not (args.min_beds <= li.beds <= args.max_beds):
            continue
        if li.baths < args.min_baths:
            continue
        if li.price > args.max_price:
            continue
        if li.zip not in set(args.zip):
            continue

        cf = run_cashflow(
            price=li.price,
            rent_monthly=args.default_rent,
            down=args.down,
            annual_rate=args.rate,
            years=args.term_years,
            tax_rate=args.tax_rate,
            insurance_monthly=args.insurance,
            reserves_pct=args.reserves_pct,
            closing_costs=0.0,
        )
        matches.append((li, cf))

    criteria = {
        "tz": args.tz,
    }
    brief = format_brief(all_msgs=len(ids), listings=matches, criteria=criteria)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(brief)

    subj = f"Morning brief (Zillow/Redfin) — {datetime.now(tz=tz.gettz(args.tz)).strftime('%Y-%m-%d')}"
    create_and_send_email(svc, args.gmail, args.to, subj, brief)


if __name__ == "__main__":
    main()
