"""
Local auto-approve webhook for testing the gate stage.

Listens on http://localhost:8080, prints every incoming trade proposal,
and responds {"approved": true} so the gate writes it to approved.json.

Usage:
    python tools/local_webhook.py          # auto-approve everything
    python tools/local_webhook.py --reject # auto-reject (tests the rejection path)

No extra dependencies — uses stdlib only.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_handler(approve: bool):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            ticker = body.get("ticker", "?")
            action = body.get("action", "?")
            strike = body.get("strike", "?")
            expiry = body.get("expiry", "?")
            contracts = body.get("suggested_contracts", "?")
            confidence = body.get("confidence", 0)

            print(f"\n{'='*60}")
            print(f"  TRADE PROPOSAL — {ticker}")
            print(f"  Action:     {action}")
            print(f"  Strike:     {strike}")
            print(f"  Expiry:     {expiry}")
            print(f"  Contracts:  {contracts}")
            print(f"  Confidence: {confidence:.0%}")
            print(f"  Reasoning:")
            for r in body.get("all_reasoning", []):
                print(f"    • {r}")
            print(f"{'='*60}")

            decision = "APPROVED" if approve else "REJECTED"
            print(f"  → Auto-{decision.lower()} (use --reject to flip)\n")

            resp = json.dumps({
                "approved": approve,
                "reason": f"local-test auto-{'approve' if approve else 'reject'}",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format, *args):
            pass  # suppress default access log noise

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reject", action="store_true", help="Auto-reject all proposals")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = HTTPServer(("localhost", args.port), make_handler(approve=not args.reject))
    mode = "REJECT" if args.reject else "APPROVE"
    print(f"Local webhook listening on http://localhost:{args.port} — will auto-{mode} all proposals")
    print("Press Ctrl+C to stop.\n")
    server.serve_forever()
