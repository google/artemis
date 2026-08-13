# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Server and cloud proxy commands (artemis server)."""

import http.server
import socketserver
import subprocess
import time
from typing import Annotated
import urllib.error
import urllib.request

from artemis.utils.logger import get_logger
import typer

logger = get_logger(__name__)
server_app = typer.Typer(help="Cloud proxy and server management commands.")

cached_token = ""
token_expire_time = 0.0


def get_identity_token() -> str:
    """Fetch Google Cloud IAM identity token for authenticated requests."""
    global cached_token, token_expire_time
    now = time.time()
    if not cached_token or now >= token_expire_time:
        try:
            res = subprocess.check_output(
                ["gcloud", "auth", "print-identity-token"], text=True
            ).strip()
            cached_token = res
            token_expire_time = now + 1800  # cache 30 mins
        except Exception as e:
            logger.warning(f"Failed to get GCP identity token: {e}")
            return ""
    return cached_token


class CloudRunProxyHandler(http.server.BaseHTTPRequestHandler):
    target_url: str = "https://artemis-cloud-brain-971929466682.us-central1.run.app"

    def log_message(self, format, *args):
        logger.info(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self):
        self.proxy_request("GET")

    def do_POST(self):
        self.proxy_request("POST")

    def do_DELETE(self):
        self.proxy_request("DELETE")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def proxy_request(self, method: str):
        url = f"{self.target_url}{self.path}"
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "authorization", "content-length"):
                headers[k] = v

        token = get_identity_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["X-Artemis-Token"] = "alice-token"

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(k, v)
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(k, v)
            data = e.read()
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Proxy Error: {e}".encode())


@server_app.command("web")
def web_proxy_command(
    port: Annotated[int, typer.Option("--port", "-p", help="Local port to bind.")] = 8080,
    target: Annotated[
        str,
        typer.Option("--target", "-t", help="Target Cloud Run URL."),
    ] = "https://artemis-cloud-brain-971929466682.us-central1.run.app",
) -> None:
    """Start the local authenticated proxy for ARTEMIS Cloud Web Dashboard."""
    CloudRunProxyHandler.target_url = target
    typer.secho(
        f"🚀 Starting Artemis Cloud Web Proxy at http://localhost:{port}", fg=typer.colors.GREEN
    )
    typer.secho(f"🔗 Forwarding authenticated requests to: {target}", fg=typer.colors.CYAN)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), CloudRunProxyHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            typer.secho("\n🛑 Stopping server.", fg=typer.colors.YELLOW)
