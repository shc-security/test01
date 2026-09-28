from pathlib import Path
from urllib.parse import urlparse

from api.analyze import handler as AnalyzeHandler


class handler(AnalyzeHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/backtest", "/backtest.html"):
            return self._send_backtest()
        return super().do_GET()

    def _send_backtest(self):
        page = Path(__file__).resolve().parent.parent / "backtest.html"
        try:
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"backtest.html을 읽지 못했습니다: {exc}"})
