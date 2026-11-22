from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"NFT Radar Bot is running!")

def run():
    server = HTTPServer(("0.0.0.0", 10000), Handler)
    server.serve_forever()

def keep_alive():
    Thread(target=run, daemon=True).start()
