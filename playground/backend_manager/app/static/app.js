// Artemis Cloud Client Workspace Application
let authToken = localStorage.getItem("artemis_jwt_token");
let currentUser = null;
let currentSession = null;
let heartbeatInterval = null;
let eventSource = null;

// DOM Elements
const authModal = document.getElementById("authModal");
const otpStep1 = document.getElementById("otpStep1");
const otpStep2 = document.getElementById("otpStep2");
const authIdentifier = document.getElementById("authIdentifier");
const otpCode = document.getElementById("otpCode");
const sendOtpBtn = document.getElementById("sendOtpBtn");
const verifyOtpBtn = document.getElementById("verifyOtpBtn");
const userBadge = document.getElementById("userBadge");
const newSessionBtn = document.getElementById("newSessionBtn");
const deviceStatus = document.getElementById("deviceStatus");
const agentStatus = document.getElementById("agentStatus");
const agentLogs = document.getElementById("agentLogs");
const streamPlaceholder = document.getElementById("streamPlaceholder");
const streamText = document.getElementById("streamText");
const taskInput = document.getElementById("taskInput");
const sendTaskBtn = document.getElementById("sendTaskBtn");

// Check Authentication on Page Load
window.addEventListener("DOMContentLoaded", async () => {
  if (authToken) {
    await verifyTokenAndInit();
  } else {
    showAuthModal();
  }
});

function showAuthModal() {
  authModal.classList.remove("hidden");
  otpStep1.classList.remove("hidden");
  otpStep2.classList.add("hidden");
}

function hideAuthModal() {
  authModal.classList.add("hidden");
}

// 1. Request OTP
sendOtpBtn.addEventListener("click", async () => {
  const ident = authIdentifier.value.trim();
  if (!ident) return alert("Please enter email or phone");

  sendOtpBtn.disabled = true;
  sendOtpBtn.innerText = "Sending Code...";

  try {
    const res = await fetch("/api/v1/auth/otp/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier: ident }),
    });
    const data = await res.json();
    if (res.ok) {
      otpStep1.classList.add("hidden");
      otpStep2.classList.remove("hidden");
      otpCode.focus();
    } else {
      alert(data.detail || "Failed to send OTP");
    }
  } catch (err) {
    alert("Network error requesting OTP");
  } finally {
    sendOtpBtn.disabled = false;
    sendOtpBtn.innerText = "Send Verification Code";
  }
});

// 2. Verify OTP Callback
verifyOtpBtn.addEventListener("click", async () => {
  const ident = authIdentifier.value.trim();
  const code = otpCode.value.trim();
  if (!code) return alert("Please enter the verification code");

  verifyOtpBtn.disabled = true;
  verifyOtpBtn.innerText = "Verifying...";

  try {
    const res = await fetch("/api/v1/auth/otp/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier: ident, code: code }),
    });
    const data = await res.json();
    if (res.ok && data.access_token) {
      authToken = data.access_token;
      localStorage.setItem("artemis_jwt_token", authToken);
      hideAuthModal();
      await verifyTokenAndInit();
    } else {
      alert(data.detail || "Invalid verification code");
    }
  } catch (err) {
    alert("Network error verifying OTP");
  } finally {
    verifyOtpBtn.disabled = false;
    verifyOtpBtn.innerText = "Verify & Launch";
  }
});

// Validate Token & Initialize User Session
async function verifyTokenAndInit() {
  try {
    const res = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error("Unauthorized");
    const user = await res.json();
    currentUser = user.user_id;

    userBadge.innerText = `👤 ${currentUser}`;
    userBadge.classList.remove("hidden");
    newSessionBtn.classList.remove("hidden");

    // Check for existing sessions or create new
    await checkActiveSessions();
  } catch (e) {
    localStorage.removeItem("artemis_jwt_token");
    authToken = null;
    showAuthModal();
  }
}

// 3. Create or Resume Session
newSessionBtn.addEventListener("click", () => createSession());

async function checkActiveSessions() {
  const res = await fetch("/api/v1/sessions", {
    headers: { Authorization: `Bearer ${authToken}` },
  });
  if (res.ok) {
    const sessions = await res.json();
    if (sessions.length > 0) {
      await attachSession(sessions[0].session_id);
      return;
    }
  }
  // Automatically create session if none active
  await createSession();
}

async function createSession() {
  appendLog("System", "Provisioning Cuttlefish emulator and starting Artemis container...");
  deviceStatus.innerText = "Provisioning...";
  agentStatus.innerText = "Starting...";

  try {
    const res = await fetch("/api/v1/sessions/create", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${authToken}`,
      },
      body: JSON.stringify({ device_preset: "pixel_7_pro", ttl_minutes: 60 }),
    });

    if (!res.ok) throw new Error("Failed to create session");
    const session = await res.json();
    await attachSession(session.session_id);
  } catch (err) {
    appendLog("Error", `Session creation failed: ${err.message}`);
    deviceStatus.innerText = "Error";
    agentStatus.innerText = "Error";
  }
}

async function attachSession(sessionId) {
  const res = await fetch(`/api/v1/sessions/${sessionId}`, {
    headers: { Authorization: `Bearer ${authToken}` },
  });
  if (!res.ok) return;

  currentSession = await res.json();
  appendLog("System", `Attached to Session ${currentSession.session_id.substring(0, 8)}`);
  appendLog("System", `ADB Connected to ${currentSession.cuttlefish_adb_endpoint}`);

  deviceStatus.innerText = "Active (Connected)";
  agentStatus.innerText = "Ready";

  // Connect live device screen stream from Artemis
  initDeviceStream(currentSession);

  // Start client heartbeat
  if (heartbeatInterval) clearInterval(heartbeatInterval);
  heartbeatInterval = setInterval(() => sendHeartbeat(sessionId), 30000);

  // Connect to Artemis SSE Stream via Nginx route
  connectArtemisStream(currentSession.artemis_stream_url);
}

function initDeviceStream(session) {
  const cuttlefishFrame = document.getElementById("cuttlefishFrame");
  const streamImg = document.getElementById("deviceStreamImg");
  const streamPlaceholder = document.getElementById("streamPlaceholder");

  if (streamPlaceholder) streamPlaceholder.classList.add("hidden");

  // Load interactive Cuttlefish WebRTC interface directly into the phone frame
  if (cuttlefishFrame) {
    cuttlefishFrame.classList.remove("hidden");
    if (streamImg) streamImg.classList.add("hidden");
    
    // Use forwarded port 9445 if accessed via 9443, or relative proxy path
    cuttlefishFrame.src = window.location.port === "9443" 
      ? "https://localhost:9445/" 
      : `${session.cuttlefish_webrtc_url}`;
  } else if (streamImg) {
    streamImg.classList.remove("hidden");
    streamImg.src = `${session.artemis_api_url}api/stream/device-live`;
  }
}

function connectArtemisStream(streamUrl) {
  if (eventSource) eventSource.close();
  try {
    eventSource = new EventSource(streamUrl);
    eventSource.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        appendLog(payload.type || "Agent", payload.message || payload.step || event.data);
      } catch {
        appendLog("Agent", event.data);
      }
    };
    eventSource.onerror = () => {
      // Stream reconnection fallback
    };
  } catch (e) {
    console.warn("SSE connection pending container boot.");
  }
}

async function sendHeartbeat(sessionId) {
  try {
    const res = await fetch(`/api/v1/sessions/${sessionId}/heartbeat`, {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (res.status === 404) {
      // Session was terminated or cleared on backend; cancel polling
      if (heartbeatInterval) {
        clearInterval(heartbeatInterval);
        heartbeatInterval = null;
      }
    }
  } catch (e) {}
}

// 4. Dispatch Task to Artemis
sendTaskBtn.addEventListener("click", () => sendTask());
taskInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendTask();
});

async function sendTask() {
  const prompt = taskInput.value.trim();
  if (!prompt || !currentSession) return;

  taskInput.value = "";
  appendLog("User", prompt);
  agentStatus.innerText = "Reasoning...";

  try {
    // Send task through Nginx routed Artemis /api/run endpoint
    const res = await fetch(`${currentSession.artemis_api_url}api/run`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ goal: prompt }),
    });

    if (res.ok) {
      appendLog("System", "Task successfully dispatched to Artemis agent.");
      agentStatus.innerText = "Executing...";
    } else {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      appendLog("System", `Error starting task: ${err.detail || res.statusText}`);
      agentStatus.innerText = "Ready";
    }
  } catch (e) {
    appendLog("System", `Connection error: ${e.message}`);
    agentStatus.innerText = "Ready";
  }
}

function appendLog(sender, text) {
  const entry = document.createElement("div");
  entry.className = `log-entry ${sender.toLowerCase()}`;
  entry.innerHTML = `<div class="meta">${sender}</div><div>${text}</div>`;
  agentLogs.appendChild(entry);
  agentLogs.scrollTop = agentLogs.scrollHeight;
}
