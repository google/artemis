package com.artemis.helper;

import android.util.Log;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Multi-threaded command server bound strictly to loopback (127.0.0.1:18888).
 *
 * Protocols:
 * - HTTP (/ping, /dump, /dump_xml, /hierarchy.xml, /snapshot, /action)
 * - Line-delimited JSON-RPC over a raw TCP socket
 *
 * Every endpoint except {@code /ping} requires the session token the host pushed
 * through {@link TokenReceiver} (header {@code X-Artemis-Token}, query parameter
 * {@code token}, or JSON field {@code token}). Loopback is reachable by every app
 * on the device, so without the token this service would hand any local app a
 * full-screen reader and a gesture injector.
 *
 * Request bodies are read as bytes (Content-Length is a byte count), so UTF-8
 * payloads of any script are handled exactly.
 */
public class CommandServer extends Thread {

    private static final String TAG = "ArtemisCommandServer";
    private static final int MAX_HEADER_LINE = 16 * 1024;
    private static final int MAX_BODY = 4 * 1024 * 1024;
    private static final String TOKEN_HEADER = "x-artemis-token";

    private final ArtemisAccessibilityService service;
    private final int port;
    private volatile boolean isRunning = true;
    private ServerSocket serverSocket;
    private final ExecutorService clientExecutor = Executors.newCachedThreadPool();

    public CommandServer(ArtemisAccessibilityService service, int port) {
        super("ArtemisCommandServer");
        this.service = service;
        this.port = port;
    }

    @Override
    public void run() {
        try {
            serverSocket = new ServerSocket();
            serverSocket.setReuseAddress(true);
            serverSocket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 50);
            Log.i(TAG, "CommandServer listening on 127.0.0.1:" + port);

            while (isRunning) {
                final Socket socket;
                try {
                    socket = serverSocket.accept();
                } catch (Exception e) {
                    if (!isRunning) break;
                    continue;
                }

                clientExecutor.execute(new Runnable() {
                    @Override
                    public void run() {
                        handleClient(socket);
                    }
                });
            }
        } catch (Exception e) {
            Log.e(TAG, "Server error", e);
        } finally {
            closeQuietly(serverSocket);
        }
    }

    // ------------------------------------------------------------------ //
    // Byte-accurate request reading
    // ------------------------------------------------------------------ //

    /** Reads one line (without CR/LF) as raw bytes; null at end of stream. */
    private static byte[] readLineBytes(InputStream in) throws Exception {
        ByteArrayOutputStream line = new ByteArrayOutputStream(256);
        int b;
        while ((b = in.read()) >= 0) {
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                line.write(b);
            }
            if (line.size() > MAX_HEADER_LINE) {
                throw new IllegalStateException("Header line too long");
            }
        }
        if (b < 0 && line.size() == 0) {
            return null;
        }
        return line.toByteArray();
    }

    private static byte[] readExactly(InputStream in, int length) throws Exception {
        byte[] buf = new byte[length];
        int total = 0;
        while (total < length) {
            int r = in.read(buf, total, length - total);
            if (r < 0) break;
            total += r;
        }
        if (total < length) {
            byte[] shorter = new byte[total];
            System.arraycopy(buf, 0, shorter, 0, total);
            return shorter;
        }
        return buf;
    }

    private void handleClient(Socket socket) {
        try {
            socket.setSoTimeout(10000);
            InputStream in = new BufferedInputStream(socket.getInputStream());
            OutputStream output = socket.getOutputStream();

            byte[] firstLine = readLineBytes(in);
            if (firstLine == null) return;
            String ascii = new String(firstLine, StandardCharsets.ISO_8859_1).trim();

            if (ascii.startsWith("GET ") || ascii.startsWith("POST ")) {
                handleHttp(ascii, in, output);
            } else if (ascii.startsWith("{")) {
                handleJsonRpc(new String(firstLine, StandardCharsets.UTF_8).trim(), output);
            } else {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "Unsupported protocol");
                sendJson(output, 400, err.toString());
            }
        } catch (Exception e) {
            Log.w(TAG, "Client handling error: " + e.getMessage());
        } finally {
            closeQuietly(socket);
        }
    }

    // ------------------------------------------------------------------ //
    // HTTP
    // ------------------------------------------------------------------ //

    private static Map<String, String> parseQuery(String query) {
        Map<String, String> params = new HashMap<>();
        if (query == null || query.isEmpty()) return params;
        for (String pair : query.split("&")) {
            if (pair.isEmpty()) continue;
            int eq = pair.indexOf('=');
            try {
                String key = URLDecoder.decode(eq < 0 ? pair : pair.substring(0, eq), "UTF-8");
                String value = eq < 0 ? "" : URLDecoder.decode(pair.substring(eq + 1), "UTF-8");
                params.put(key, value);
            } catch (Exception ignored) {}
        }
        return params;
    }

    private void handleHttp(String requestLine, InputStream in, OutputStream output) {
        try {
            String[] parts = requestLine.split(" ");
            String target = parts.length > 1 ? parts[1] : "/";
            String path = target;
            Map<String, String> query;
            int q = target.indexOf('?');
            if (q >= 0) {
                path = target.substring(0, q);
                query = parseQuery(target.substring(q + 1));
            } else {
                query = new HashMap<>();
            }

            int contentLength = 0;
            String headerToken = null;
            byte[] lineBytes;
            while ((lineBytes = readLineBytes(in)) != null) {
                if (lineBytes.length == 0) break;
                String line = new String(lineBytes, StandardCharsets.ISO_8859_1);
                int colon = line.indexOf(':');
                if (colon < 0) continue;
                String name = line.substring(0, colon).trim().toLowerCase(Locale.ROOT);
                String value = line.substring(colon + 1).trim();
                if (name.equals("content-length")) {
                    try {
                        contentLength = Integer.parseInt(value);
                    } catch (Exception ignored) {}
                } else if (name.equals(TOKEN_HEADER)) {
                    headerToken = value;
                }
            }
            if (contentLength < 0 || contentLength > MAX_BODY) {
                sendJson(output, 413, errorJson("Body too large").toString());
                return;
            }

            String body = "";
            if (contentLength > 0) {
                body = new String(readExactly(in, contentLength), StandardCharsets.UTF_8);
            }

            String token = headerToken != null ? headerToken : query.get("token");
            boolean authed = TokenStore.matches(token);

            if (path.equals("/ping") || path.equals("/")) {
                sendJson(output, 200, buildPing(authed).toString());
                return;
            }
            if (!authed) {
                sendJson(output, 401, unauthorizedJson().toString());
                return;
            }

            // Route endpoints
            if (path.equals("/snapshot")) {
                HierarchyDumper.DumpOptions options = HierarchyDumper.DumpOptions.forSnapshot().apply(query);
                JSONObject json = HierarchyDumper.dumpAtomicSnapshot(service, options);
                sendJson(output, 200, json.toString());
            } else if (path.equals("/dump_xml") || path.equals("/hierarchy.xml")
                    || (path.equals("/dump") && "xml".equals(query.get("format")))) {
                HierarchyDumper.DumpOptions options = HierarchyDumper.DumpOptions.forSnapshot().apply(query);
                sendXml(output, HierarchyDumper.dumpXml(service, options));
            } else if (path.equals("/dump") || path.equals("/hierarchy")) {
                HierarchyDumper.DumpOptions options = HierarchyDumper.DumpOptions.forDump().apply(query);
                sendJson(output, 200, HierarchyDumper.dump(service, options).toString());
            } else if (path.equals("/action") || path.equals("/rpc")) {
                JSONObject json = body.isEmpty() ? new JSONObject() : new JSONObject(body);
                JSONObject resp = executeCommand(json.optString("cmd", ""), json);
                sendJson(output, 200, resp.toString());
            } else {
                sendJson(output, 404, errorJson("Unknown endpoint: " + path).toString());
            }

        } catch (Exception e) {
            Log.e(TAG, "HTTP error", e);
            try {
                sendJson(output, 500, errorJson("Internal error: " + e.getMessage()).toString());
            } catch (Throwable ignored) {}
        }
    }

    // ------------------------------------------------------------------ //
    // Raw JSON-RPC
    // ------------------------------------------------------------------ //

    private void handleJsonRpc(String line, OutputStream output) {
        try {
            JSONObject json = new JSONObject(line);
            String cmd = json.optString("cmd", "");
            boolean authed = TokenStore.matches(json.optString("token", null));
            JSONObject resp;
            if (cmd.equalsIgnoreCase("ping")) {
                resp = buildPing(authed);
            } else if (!authed) {
                resp = unauthorizedJson();
            } else {
                resp = executeCommand(cmd, json);
            }
            byte[] bytes = resp.toString().getBytes(StandardCharsets.UTF_8);
            output.write(bytes);
            output.write('\n');
            output.flush();
        } catch (Exception e) {
            try {
                JSONObject err = errorJson("JSON parse error: " + e.getMessage());
                output.write(err.toString().getBytes(StandardCharsets.UTF_8));
                output.write('\n');
                output.flush();
            } catch (Exception ignored) {}
        }
    }

    // ------------------------------------------------------------------ //
    // Commands
    // ------------------------------------------------------------------ //

    private JSONObject executeCommand(String cmd, JSONObject params) {
        JSONObject resp = new JSONObject();
        try {
            switch (cmd.toLowerCase(Locale.ROOT)) {
                case "ping":
                    return buildPing(true);
                case "dump":
                case "dump_ui":
                    return HierarchyDumper.dump(service, HierarchyDumper.DumpOptions.forDump());
                case "snapshot":
                    return HierarchyDumper.dumpAtomicSnapshot(service, HierarchyDumper.DumpOptions.forSnapshot());
                case "dump_xml":
                    resp.put("success", true);
                    resp.put("xml", HierarchyDumper.dumpXml(service));
                    break;
                case "tap":
                    float x = (float) params.optDouble("x", -1.0);
                    float y = (float) params.optDouble("y", -1.0);
                    long tapTimeout = params.optLong("timeout", 1500L);
                    if (x < 0 || y < 0) {
                        resp.put("success", false);
                        resp.put("error", "Invalid coordinates");
                    } else {
                        resp.put("success", GestureController.tap(service, x, y, tapTimeout));
                    }
                    break;
                case "double_tap":
                    float dtx = (float) params.optDouble("x", -1.0);
                    float dty = (float) params.optDouble("y", -1.0);
                    long dtTimeout = params.optLong("timeout", 2000L);
                    if (dtx < 0 || dty < 0) {
                        resp.put("success", false);
                        resp.put("error", "Invalid coordinates");
                    } else {
                        resp.put("success", GestureController.doubleTap(service, dtx, dty, dtTimeout));
                    }
                    break;
                case "long_press":
                    float lpx = (float) params.optDouble("x", -1.0);
                    float lpy = (float) params.optDouble("y", -1.0);
                    long lpDuration = params.optLong("duration", 1000L);
                    if (lpx < 0 || lpy < 0) {
                        resp.put("success", false);
                        resp.put("error", "Invalid coordinates");
                    } else {
                        resp.put("success", GestureController.longPress(service, lpx, lpy, lpDuration, 2500L));
                    }
                    break;
                case "swipe":
                    float x1 = (float) params.optDouble("x1", -1.0);
                    float y1 = (float) params.optDouble("y1", -1.0);
                    float x2 = (float) params.optDouble("x2", -1.0);
                    float y2 = (float) params.optDouble("y2", -1.0);
                    long dur = params.optLong("duration", 300L);
                    resp.put("success", GestureController.swipe(service, x1, y1, x2, y2, dur, 3000L));
                    break;
                case "type":
                    String text = params.optString("text", "");
                    boolean append = params.optBoolean("append", false);
                    resp.put("success", GestureController.setText(service, text, append));
                    break;
                case "clear":
                    resp.put("success", GestureController.clearText(service));
                    break;
                case "clipboard":
                    resp.put("success", GestureController.setClipboard(service, params.optString("text", "")));
                    break;
                case "global":
                    String action = params.optString("action", "");
                    resp.put("success", GestureController.performGlobalAction(service, action));
                    break;
                default:
                    resp.put("success", false);
                    resp.put("error", "Unknown command: " + cmd);
                    break;
            }
        } catch (Exception e) {
            try {
                resp.put("success", false);
                resp.put("error", e.getMessage());
            } catch (Exception ignored) {}
        }
        return resp;
    }

    /**
     * Health payload shared by GET /ping and the "ping" RPC. The host compares
     * version_code / protocol_version against the bundled APK to decide whether
     * to upgrade, and token_set to decide whether to push its token. The
     * foreground package and activity are only disclosed to an authenticated caller.
     */
    private JSONObject buildPing(boolean authed) throws Exception {
        JSONObject r = new JSONObject();
        r.put("success", true);
        r.put("service", "ArtemisAccessibilityService");
        r.put("version_code", service.getVersionCode());
        r.put("version_name", service.getVersionName());
        r.put("protocol_version", ArtemisAccessibilityService.PROTOCOL_VERSION);
        r.put("port", port);
        r.put("auth_required", true);
        r.put("token_set", TokenStore.isSet());
        r.put("authenticated", authed);
        if (authed) {
            r.put("package", service.getCurrentPackageName());
            r.put("activity", service.getCurrentActivityName());
        }
        return r;
    }

    private static JSONObject errorJson(String message) throws Exception {
        JSONObject r = new JSONObject();
        r.put("success", false);
        r.put("error", message);
        return r;
    }

    private static JSONObject unauthorizedJson() throws Exception {
        JSONObject r = errorJson(TokenStore.isSet()
                ? "unauthorized: wrong or missing X-Artemis-Token"
                : "unauthorized: no session token has been pushed to the helper yet");
        r.put("token_set", TokenStore.isSet());
        return r;
    }

    // ------------------------------------------------------------------ //
    // Responses
    // ------------------------------------------------------------------ //

    private static String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 404: return "Not Found";
            case 413: return "Payload Too Large";
            default: return "Internal Server Error";
        }
    }

    private static void send(OutputStream output, int status, String contentType, byte[] bytes) {
        try {
            String header = "HTTP/1.1 " + status + " " + reason(status) + "\r\n" +
                    "Content-Type: " + contentType + "; charset=utf-8\r\n" +
                    "Content-Length: " + bytes.length + "\r\n" +
                    "Connection: close\r\n\r\n";
            output.write(header.getBytes(StandardCharsets.UTF_8));
            output.write(bytes);
            output.flush();
        } catch (Exception ignored) {}
    }

    private static void sendJson(OutputStream output, int status, String jsonStr) {
        send(output, status, "application/json", jsonStr.getBytes(StandardCharsets.UTF_8));
    }

    private static void sendXml(OutputStream output, String xmlStr) {
        send(output, 200, "application/xml", xmlStr.getBytes(StandardCharsets.UTF_8));
    }

    public void shutdown() {
        isRunning = false;
        closeQuietly(serverSocket);
        clientExecutor.shutdownNow();
    }

    private static void closeQuietly(Closeable c) {
        if (c != null) {
            try { c.close(); } catch (Throwable ignored) {}
        }
    }
}
