package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.WindowManager;
import android.view.ViewConfiguration;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.TreeMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** One bounded transaction owns every contact until the final phase lifts it. */
public final class ContinuousGesture {
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final LinkedHashSet<String> CANCELLED = new LinkedHashSet<>();
    private static ContinuousGesture active;
    private final AccessibilityService service;
    private final String requestId;
    private final List<Phase> phases;
    private final CountDownLatch done = new CountDownLatch(1);
    private volatile boolean cancelRequested;
    private volatile JSONObject result;
    private volatile int completed;
    private boolean dispatched;
    private boolean releasing;

    static final class Phase {
        final long duration;
        final TreeMap<Integer, Path> paths;
        final TreeMap<Integer, float[]> ends;
        GestureDescription.StrokeDescription[] strokes;
        GestureDescription gesture;
        Phase(long duration, TreeMap<Integer, Path> paths, TreeMap<Integer, float[]> ends) {
            this.duration = duration; this.paths = paths; this.ends = ends;
        }
    }

    private ContinuousGesture(AccessibilityService service, String requestId, List<Phase> phases) {
        this.service = service; this.requestId = requestId; this.phases = phases;
    }

    static JSONObject reply(String id, String status, int count, boolean released, String error) {
        JSONObject result = new JSONObject();
        try {
            result.put("request_id", id);
            result.put("success", "completed".equals(status));
            result.put("status", status);
            result.put("phases_completed", count);
            result.put("release_confirmed", released);
            if (error != null) result.put("error", error);
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
        return result;
    }

    private static int integer(Object value, int min, int max) {
        if (!(value instanceof Integer || value instanceof Long))
            throw new IllegalArgumentException("Expected an integer");
        long n = ((Number) value).longValue();
        if (n < min || n > max) throw new IllegalArgumentException("Integer outside allowed range");
        return (int) n;
    }

    // Validate the ENTIRE plan before building or dispatching any input.
    static List<Phase> parse(JSONArray input, int width, int height) throws Exception {
        if (width < 1 || height < 1 || input == null || input.length() < 1 || input.length() > 32)
            throw new IllegalArgumentException("Invalid display or phase count");
        if ("long_press_drag".equals(input.getJSONObject(0).optString("kind"))) {
            if (input.length() != 1)
                throw new IllegalArgumentException("long_press_drag must be the only entry");
            input = LongPressDrag.expand(input.getJSONObject(0),
                    ViewConfiguration.getLongPressTimeout());
        }
        if (input.length() > 1 && Build.VERSION.SDK_INT < 26)
            throw new UnsupportedOperationException("Continuous gestures require Android 8.0+");
        List<Phase> phases = new ArrayList<>();
        TreeMap<Integer, String> previousEnds = null;
        long total = 0;
        for (int i = 0; i < input.length(); i++) {
            JSONObject raw = input.getJSONObject(i);
            if (raw.length() != 2) throw new IllegalArgumentException("Unknown phase fields");
            int duration = integer(raw.get("duration_ms"), 1, 5000);
            total += duration;
            if (total > 30000 || total > GestureDescription.getMaxGestureDuration())
                throw new IllegalArgumentException("Gesture exceeds duration limit");
            JSONArray pointers = raw.getJSONArray("pointers");
            if (pointers.length() < 1 || pointers.length() > Math.min(10, GestureDescription.getMaxStrokeCount()))
                throw new IllegalArgumentException("Invalid pointer count");
            TreeMap<Integer, Path> paths = new TreeMap<>();
            TreeMap<Integer, float[]> ends = new TreeMap<>();
            TreeMap<Integer, String> normalizedEnds = new TreeMap<>();
            for (int j = 0; j < pointers.length(); j++) {
                JSONObject pointer = pointers.getJSONObject(j);
                boolean curved = pointer.has("control_points");
                if (pointer.length() != (curved ? 3 : 2))
                    throw new IllegalArgumentException("Unknown pointer fields");
                int id = integer(pointer.get("id"), 0, 9);
                if (paths.containsKey(id)) throw new IllegalArgumentException("Duplicate pointer ID");
                JSONArray points = pointer.getJSONArray("path");
                if (points.length() < 1 || points.length() > 128)
                    throw new IllegalArgumentException("Invalid path length");
                float[] controls = null;
                if (curved) {
                    JSONArray rawControls = pointer.getJSONArray("control_points");
                    if (points.length() != 2 || rawControls.length() != 2)
                        throw new IllegalArgumentException("Cubic Bezier requires two endpoints and two control points");
                    controls = new float[4];
                    for (int c = 0; c < 2; c++) {
                        JSONArray point = rawControls.getJSONArray(c);
                        if (point.length() != 2) throw new IllegalArgumentException("Expected control [x,y]");
                        controls[c * 2] = integer(point.get(0), 0, 1000) * (width - 1) / 1000f;
                        controls[c * 2 + 1] = integer(point.get(1), 0, 1000) * (height - 1) / 1000f;
                    }
                }
                Path path = new Path();
                for (int k = 0; k < points.length(); k++) {
                    JSONArray point = points.getJSONArray(k);
                    if (point.length() != 2) throw new IllegalArgumentException("Expected [x,y]");
                    int nx = integer(point.get(0), 0, 1000);
                    int ny = integer(point.get(1), 0, 1000);
                    String normalized = nx + "," + ny;
                    if (k == 0 && previousEnds != null && !normalized.equals(previousEnds.get(id)))
                        throw new IllegalArgumentException("Discontinuous pointer path");
                    float x = nx * (width - 1) / 1000f, y = ny * (height - 1) / 1000f;
                    if (k == 0) path.moveTo(x, y);
                    else if (curved) path.cubicTo(controls[0], controls[1], controls[2], controls[3], x, y);
                    else path.lineTo(x, y);
                    ends.put(id, new float[]{x, y});
                    normalizedEnds.put(id, normalized);
                }
                paths.put(id, path);
            }
            if (previousEnds != null && !previousEnds.keySet().equals(paths.keySet()))
                throw new IllegalArgumentException("Keep the same pointer IDs across phases");
            previousEnds = normalizedEnds;
            phases.add(new Phase(duration, paths, ends));
        }
        GestureDescription.StrokeDescription[] prior = null;
        for (int i = 0; i < phases.size(); i++) {
            Phase phase = phases.get(i);
            boolean more = i + 1 < phases.size();
            phase.strokes = new GestureDescription.StrokeDescription[phase.paths.size()];
            GestureDescription.Builder builder = new GestureDescription.Builder();
            int j = 0;
            for (Path path : phase.paths.values()) {
                GestureDescription.StrokeDescription stroke;
                if (prior != null) stroke = prior[j].continueStroke(path, 0, phase.duration, more);
                else if (Build.VERSION.SDK_INT >= 26) stroke = new GestureDescription.StrokeDescription(path, 0, phase.duration, more);
                else stroke = new GestureDescription.StrokeDescription(path, 0, phase.duration);
                phase.strokes[j++] = stroke;
                builder.addStroke(stroke);
            }
            phase.gesture = builder.build();
            prior = phase.strokes;
        }
        return phases;
    }

    public static JSONObject execute(AccessibilityService service, JSONObject params) {
        String id = params.optString("request_id", "");
        if (!id.matches("[A-Za-z0-9_-]{1,64}"))
            return reply(id, "invalid_arguments", 0, true, "A bounded request_id is required");
        if (Looper.myLooper() == Looper.getMainLooper())
            return reply(id, "rejected", 0, true, "Call from a worker thread");
        final ContinuousGesture run;
        try {
            DisplayMetrics metrics = new DisplayMetrics();
            ((WindowManager) service.getSystemService(AccessibilityService.WINDOW_SERVICE))
                    .getDefaultDisplay().getRealMetrics(metrics);
            run = new ContinuousGesture(service, id,
                    parse(params.getJSONArray("phases"), metrics.widthPixels, metrics.heightPixels));
        } catch (UnsupportedOperationException e) {
            return reply(id, "unsupported", 0, true, e.getMessage());
        } catch (Exception e) {
            return reply(id, "invalid_arguments", 0, true, e.getMessage());
        }
        synchronized (ContinuousGesture.class) {
            if (CANCELLED.remove(id)) return reply(id, "cancelled", 0, true, "Cancelled before dispatch");
            if (!GestureController.INPUT_BUSY.compareAndSet(false, true))
                return reply(id, "busy", 0, true, "Another gesture is active or needs recovery");
            active = run;
        }
        long total = 0;
        for (Phase p : run.phases) total += p.duration;
        MAIN.post(() -> run.next());
        try {
            if (run.done.await(total + 5000, TimeUnit.MILLISECONDS)) return run.result;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        run.cancelRequested = true;
        // Do not unlock or replay: late callbacks still own and release held contacts.
        return reply(id, "timeout", run.completed, false,
                "Gesture outcome unknown; cancellation requested. Reconcile device before reuse.");
    }

    public static synchronized JSONObject cancel(String id) {
        if (id == null || !id.matches("[A-Za-z0-9_-]{1,64}"))
            return reply(id, "invalid_arguments", 0, true, "Invalid request_id");
        if (active != null && active.requestId.equals(id)) {
            active.cancelRequested = true;
            return reply(id, "cancelling", active.completed, false, "Will release after the current bounded phase");
        }
        // Covers cancellation racing the host's in-flight capability check / request.
        CANCELLED.add(id);
        while (CANCELLED.size() > 128) CANCELLED.remove(CANCELLED.iterator().next());
        return reply(id, "cancelled", 0, true, "No matching active gesture");
    }

    private void finish(String status, boolean released, String error) {
        if (result != null) return;
        result = reply(requestId, status, completed, released, error);
        synchronized (ContinuousGesture.class) {
            if (active == this) active = null;
        }
        // Unknown held contacts fence all further injected gestures until recovery.
        if (released || !dispatched) GestureController.INPUT_BUSY.set(false);
        done.countDown();
    }

    private void next() {
        if (result != null) return;
        if (cancelRequested) { release("cancelled"); return; }
        if (completed == phases.size()) { finish("completed", true, null); return; }
        Phase phase = phases.get(completed);
        final long phaseEndsAt = SystemClock.uptimeMillis() + phase.duration;
        try {
            boolean accepted = service.dispatchGesture(phase.gesture,
                    new AccessibilityService.GestureResultCallback() {
                        @Override public void onCompleted(GestureDescription ignored) {
                            completed++;
                            if (completed == phases.size())
                                finish(cancelRequested ? "cancelled" : "completed", true,
                                        cancelRequested ? "Cancellation requested" : null);
                            else {
                                // Android completes a stationary continued stroke after DOWN:
                                // duplicate MOVE events are omitted and there is no UP yet.
                                // Keep contact held until its requested time before continuing.
                                MAIN.postAtTime(() -> next(), phaseEndsAt);
                            }
                        }
                        @Override public void onCancelled(GestureDescription ignored) {
                            // Android cancels this stream. Conservatively do not certify release.
                            finish("cancelled", false, "System cancelled gesture; reconcile device");
                        }
                    }, MAIN);
            if (!accepted) release("rejected"); else dispatched = true;
        } catch (Exception e) { release("rejected"); }
    }

    private void release(final String status) {
        if (completed == 0 || completed == phases.size()) {
            finish(status, !dispatched || completed == phases.size(), "Gesture " + status);
            return;
        }
        if (releasing) return;
        releasing = true;
        try {
            Phase previous = phases.get(completed - 1);
            GestureDescription.Builder builder = new GestureDescription.Builder();
            int j = 0;
            for (float[] endpoint : previous.ends.values()) {
                Path still = new Path(); still.moveTo(endpoint[0], endpoint[1]);
                builder.addStroke(previous.strokes[j++].continueStroke(still, 0, 1, false));
            }
            boolean accepted = service.dispatchGesture(builder.build(),
                    new AccessibilityService.GestureResultCallback() {
                        @Override public void onCompleted(GestureDescription ignored) {
                            finish(status, true, "Gesture " + status + "; held contacts released");
                        }
                        @Override public void onCancelled(GestureDescription ignored) {
                            finish(status, false, "Release cancelled; reconcile device");
                        }
                    }, MAIN);
            if (!accepted) finish(status, false, "Release rejected; reconcile device");
        } catch (Exception e) { finish(status, false, "Release failed; reconcile device"); }
    }
}
