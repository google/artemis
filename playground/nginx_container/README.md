# 🌐 Artemis Ingress Reverse Proxy Container

Production-ready, high-performance Nginx Reverse Proxy container designed for running Artemis on a **Container-Optimized OS (COS)** VM in Google Cloud.

---

## 📌 Architecture & Routing Matrix

The proxy acts as the single TLS/SSL termination gateway on standard ports (`80` $\rightarrow$ `443`), dynamically routing traffic to persistent platform services and on-demand ephemeral Artemis session containers without needing Nginx reload or restart:

| Path Prefix | Destination Upstream | Protocol | Purpose |
| :--- | :--- | :--- | :--- |
| `GET /healthz` | Nginx Internal | HTTP/HTTPS | Ingress health check probe |
| `/` | `http://backend:8000/` | HTTP/1.1 | Static frontend Single Page Application (React/Vue/Angular) |
| `/api/*` | `http://backend:8000/api/*` | HTTP/1.1 / WS | Management API, OTP auth verification, session lifecycle |
| `/session/{id}/artemis/stream/*` | `http://artemis-session-{id}:8080/stream/*` | **SSE / WebSocket** | Real-time agent thought stream (zero buffering, 1h timeout) |
| `/session/{id}/artemis/*` | `http://artemis-session-{id}:8080/*` | HTTP/1.1 / WS | Artemis REST API and task execution endpoints |
| `/session/{id}/cuttlefish/*` | `http://host.docker.internal:8443/*` | **WebRTC / WS** | Cuttlefish WebRTC signaling & interactive device controls |

---

## 📁 File Structure

```
playground/nginx_container/
├── Dockerfile                   # Nginx alpine image with openssl and health checks
├── docker-entrypoint.sh         # Auto-generates dev certificates & tests nginx syntax
├── docker-compose.yml           # Compose specification with artemis-net bridge network
├── .dockerignore
├── README.md                    # Detailed operational documentation
├── certs/
│   ├── generate_dev_certs.sh    # Manual SSL certificate generation helper
│   └── .gitkeep
├── conf/
│   ├── nginx.conf               # Master Nginx configuration (JSON logging, WebSocket map)
│   ├── conf.d/
│   │   ├── http_redirect.conf   # Port 80 redirect to Port 443 + ACME challenge
│   │   └── artemis_ssl.conf     # Port 443 SSL server block & dynamic Docker DNS routing
│   └── includes/
│       ├── proxy_headers.conf   # Standard forwarding headers (Host, X-Real-IP, X-Forwarded-*)
│       ├── websocket_params.conf# WebSocket upgrade headers & 3600s timeouts
│       └── sse_params.conf      # SSE streaming parameters (proxy_buffering off)
└── html/
    ├── index.html               # Fallback landing page
    ├── 502.json                 # JSON error response for provisioning containers
    └── 504.json                 # JSON error response for timeouts
```

---

## 🚀 Quick Start

### 1. Build the Docker Image
```bash
docker build -t artemis-nginx-proxy:latest .
```

### 2. Run with Docker Compose
```bash
docker-compose up -d
```

### 3. Run Standalone on COS VM
```bash
# Create shared bridge network if not exists
docker network create artemis-net || true

# Run Nginx Reverse Proxy Container
docker run -d \
  --name artemis-nginx-proxy \
  --restart unless-stopped \
  --network artemis-net \
  --add-host host.docker.internal:host-gateway \
  -p 80:80 \
  -p 443:443 \
  -v $(pwd)/certs:/etc/nginx/certs:ro \
  artemis-nginx-proxy:latest
```

---

## 🔑 Key Features & Technical Highlights

### 1. Dynamic Zero-Reload Upstream Resolution
When a user launches a new session, the backend starts a new Docker container named `artemis-session-<session_id>` on the `artemis-net` network.

Using Docker's embedded DNS server (`127.0.0.11 valid=5s`) combined with variable-based `proxy_pass`:
```nginx
resolver 127.0.0.11 valid=5s ipv6=off;

location ~ ^/session/(?<session_id>[a-zA-Z0-9_-]+)/artemis/(?<artemis_path>.*)$ {
    set $artemis_host "artemis-session-$session_id";
    proxy_pass http://$artemis_host:8080/$artemis_path$is_args$args;
}
```
Nginx resolves the container hostname at **request-time**, allowing session containers to be created and destroyed without touching or reloading Nginx configuration.

### 2. Real-Time Streaming (SSE & WebSocket)
- **SSE Streams** (`/stream/*`): Uses `proxy_buffering off;`, `proxy_cache off;`, and `chunked_transfer_encoding on;` so the browser receives agent reasoning tokens with zero buffering delay.
- **WebRTC Signaling & WebSockets**: Employs `map $http_upgrade $connection_upgrade` with 1-hour idle timeouts (`proxy_read_timeout 3600s;`).

### 3. Automated SSL/TLS Management
- **Development/Staging**: The container entrypoint automatically generates a self-signed certificate if no certificates exist in `/etc/nginx/certs/`.
- **Production**: Mount your Let's Encrypt or GCP managed certificates to `/etc/nginx/certs/tls.crt` and `/etc/nginx/certs/tls.key`.

### 4. Resilient Error Handling
If a requested session container is still booting or unavailable, Nginx returns a clean JSON error response (`502.json` / `504.json`) instead of standard Nginx HTML error pages, enabling the frontend UI to gracefully poll or display a loading spinner.

---

## 🧪 Verification & Testing

1. **Verify Health Endpoint**:
   ```bash
   curl -k https://localhost/healthz
   # Output: OK
   ```

2. **Test Dynamic Routing**:
   ```bash
   # Launch a mock session container
   docker run -d --name artemis-session-test123 --network artemis-net hashicorp/http-echo -text="Hello from Session test123" -listen=:8080

   # Test via Nginx proxy
   curl -k https://localhost/session/test123/artemis/
   # Output: Hello from Session test123
   ```
