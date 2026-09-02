# 🤖 Artemis Session Container

Dedicated, session-isolated container image for running the Artemis Autonomous AI Agent on Google Cloud **Container-Optimized OS (COS)**.

---

## 📌 Architecture & Lifecycle

When a user initiates a session via the [Backend Manager](../backend_manager/README.md), the backend dynamically spawns an instance of this container named `artemis-session-<session_id>` on the shared `artemis-net` bridge network.

---

## 🔑 Key Features

1. **Automated ADB Bridging**:
   - The container entrypoint automatically connects to the target Cuttlefish instance with automatic retry handling.

2. **Multimodal Perception & Toolchain**:
   - Packaged with Android Platform Tools, ffmpeg, OpenCV libraries, and Python.

3. **Real-time Event Streaming**:
   - Hosts the Artemis Admin Console server exposing SSE and WebSocket endpoints for live streaming agent reasoning, thought steps, and action execution directly to the frontend.

4. **Dynamic Selector & Coordinate Fallback Engine**:
   - Drives real-time automated navigation on the connected Cuttlefish emulator using Artemis's multimodal reasoning pipeline.

---

## 📁 Directory Structure

```
playground/artemis_container/
├── Dockerfile                   # Python 3.12 slim with adb, ffmpeg, OpenCV, and Artemis
├── docker-entrypoint.sh         # Automated ADB connection loop & service startup
├── docker-compose.yml           # Compose spec for testing standalone session
├── .dockerignore
└── README.md                    # Documentation & operational guide
```

---

## 🚀 Building & Running

### 1. Build the Artemis Container Image
From the repository root:
```bash
docker build -f playground/artemis_container/Dockerfile -t artemis:latest .
```

### 2. Run Standalone with Docker CLI
```bash
# Ensure bridge network exists
docker network create artemis-net || true

# Run Artemis session container targeting Cuttlefish on port 6520
docker run -d \
  --name artemis-session-sample \
  --network artemis-net \
  --add-host host.docker.internal:host-gateway \
  -e SESSION_ID=sample-123 \
  -e ADB_DEVICE_SERIAL=host.docker.internal:6520 \
  -e GEMINI_API_KEY="your-gemini-key" \
  -p 8080:8080 \
  artemis:latest
```

### 3. Run with Docker Compose (Test Setup)
```bash
cd playground/artemis_container
docker-compose up -d
```

---

## 🧪 Verification & Health Check

1. **Verify ADB Connection inside Container**:
   ```bash
   docker exec -it artemis-session-sample adb devices
   ```

2. **Verify Server Health Probe**:
   ```bash
   curl http://localhost:8080/api/system/health
   # Returns: {"status": "ok", ...}
   ```

3. **Verify Trace Streaming**:
   ```bash
   curl -N http://localhost:8080/stream/events
   # Real-time Server-Sent Events stream
   ```
