package com.artemis.helper;

import android.util.Log;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.Closeable;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * High-performance, multi-threaded command server bound strictly to loopback (127.0.0.1:18888).
 * Supports:
 * - HTTP REST API (/dump, /dump_xml, /hierarchy.xml, /ping, /action)
 * - Line-delimited JSON-RPC over TCP socket
 */
public class CommandServer extends Thread {

    private static final String TAG = "ArtemisCommandServer";
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

    private void handleClient(Socket socket) {
        try {
            socket.setSoTimeout(10000);
            BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
            OutputStream output = socket.getOutputStream();

            String firstLine = reader.readLine();
            if (firstLine == null) return;
            String trimmed = firstLine.trim();

            if (trimmed.startsWith("GET ") || trimmed.startsWith("POST ")) {
                handleHttp(trimmed, reader, output);
            } else if (trimmed.startsWith("{")) {
                handleJsonRpc(trimmed, output);
            } else {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "Unsupported protocol");
                sendJsonResponse(output, err.toString());
            }
        } catch (Exception e) {
            Log.w(TAG, "Client handling error: " + e.getMessage());
        } finally {
            closeQuietly(socket);
        }
    }

    private void handleHttp(String requestLine, BufferedReader reader, OutputStream output) {
        try {
            String[] parts = requestLine.split(" ");
            String path = parts.length > 1 ? parts[1] : "/";

            int contentLength = 0;
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.isEmpty()) break;
                String lower = line.toLowerCase();
                if (lower.startsWith("content-length:")) {
                    try {
                        contentLength = Integer.parseInt(lower.substring("content-length:".length()).trim());
                    } catch (Exception ignored) {}
                }
            }

            String body = "";
            if (contentLength > 0) {
                char[] buf = new char[contentLength];
                int total = 0;
                while (total < contentLength) {
                    int r = reader.read(buf, total, contentLength - total);
                    if (r < 0) break;
                    total += r;
                }
                body = new String(buf, 0, total);
            }

            // Route endpoints
            if (path.startsWith("/snapshot")) {
                JSONObject json = HierarchyDumper.dumpAtomicSnapshot(service);
                sendJsonResponse(output, json.toString());
            } else if (path.equals("/dump_xml") || path.equals("/hierarchy.xml") || path.startsWith("/dump?format=xml")) {
                String xml = HierarchyDumper.dumpXml(service);
                sendXmlResponse(output, xml);
            } else if (path.startsWith("/dump") || path.startsWith("/hierarchy")) {
                JSONObject json = HierarchyDumper.dump(service);
                sendJsonResponse(output, json.toString());
            } else if (path.startsWith("/ping") || path.equals("/")) {
                JSONObject r = new JSONObject();
                r.put("success", true);
                r.put("service", "ArtemisAccessibilityService");
                r.put("package", service.getCurrentPackageName());
                r.put("activity", service.getCurrentActivityName());
                sendJsonResponse(output, r.toString());
            } else if (path.startsWith("/action") || path.startsWith("/rpc")) {
                JSONObject json = body.isEmpty() ? new JSONObject() : new JSONObject(body);
                JSONObject resp = executeCommand(json.optString("cmd", ""), json);
                sendJsonResponse(output, resp.toString());
            } else {
                JSONObject r = new JSONObject();
                r.put("success", false);
                r.put("error", "Unknown endpoint: " + path);
                sendJsonResponse(output, r.toString());
            }

        } catch (Exception e) {
            Log.e(TAG, "HTTP error", e);
        }
    }

    private void handleJsonRpc(String line, OutputStream output) {
        try {
            JSONObject json = new JSONObject(line);
            String cmd = json.optString("cmd", "");
            JSONObject resp = executeCommand(cmd, json);
            byte[] bytes = resp.toString().getBytes(StandardCharsets.UTF_8);
            output.write(bytes);
            output.write('\n');
            output.flush();
        } catch (Exception e) {
            try {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "JSON parse error: " + e.getMessage());
                output.write(err.toString().getBytes(StandardCharsets.UTF_8));
                output.write('\n');
                output.flush();
            } catch (Exception ignored) {}
        }
    }

    private JSONObject executeCommand(String cmd, JSONObject params) {
        JSONObject resp = new JSONObject();
        try {
            switch (cmd.toLowerCase()) {
                case "ping":
                    resp.put("success", true);
                    resp.put("service", "ArtemisAccessibilityService");
                    resp.put("package", service.getCurrentPackageName());
                    resp.put("activity", service.getCurrentActivityName());
                    break;
                case "dump":
                case "dump_ui":
                    return HierarchyDumper.dump(service);
                case "snapshot":
                    return HierarchyDumper.dumpAtomicSnapshot(service);
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
                    resp.put("success", GestureController.setText(service, text));
                    break;
                case "clear":
                    resp.put("success", GestureController.clearText(service));
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

    private void sendJsonResponse(OutputStream output, String jsonStr) {
        try {
            byte[] bytes = jsonStr.getBytes(StandardCharsets.UTF_8);
            String header = "HTTP/1.1 200 OK\r\n" +
                    "Content-Type: application/json; charset=utf-8\r\n" +
                    "Content-Length: " + bytes.length + "\r\n" +
                    "Connection: close\r\n\r\n";
            output.write(header.getBytes(StandardCharsets.UTF_8));
            output.write(bytes);
            output.flush();
        } catch (Exception ignored) {}
    }

    private void sendXmlResponse(OutputStream output, String xmlStr) {
        try {
            byte[] bytes = xmlStr.getBytes(StandardCharsets.UTF_8);
            String header = "HTTP/1.1 200 OK\r\n" +
                    "Content-Type: application/xml; charset=utf-8\r\n" +
                    "Content-Length: " + bytes.length + "\r\n" +
                    "Connection: close\r\n\r\n";
            output.write(header.getBytes(StandardCharsets.UTF_8));
            output.write(bytes);
            output.flush();
        } catch (Exception ignored) {}
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
