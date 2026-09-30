package com.artemis.helper;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONObject;

/** Expands a long-press drag; ContinuousGesture owns dispatch and release. */
final class LongPressDrag {
    private static final Set<String> FIELDS = new HashSet<>(Arrays.asList(
            "kind", "start", "end", "control_points", "duration_ms", "hold_ms", "release_delay_ms"));

    private static int integer(Object value, int min, int max) {
        if (!(value instanceof Integer || value instanceof Long))
            throw new IllegalArgumentException("Expected an integer");
        long n = ((Number) value).longValue();
        if (n < min || n > max) throw new IllegalArgumentException("Integer outside allowed range");
        return (int) n;
    }

    private static JSONArray point(JSONArray value) throws Exception {
        if (value.length() != 2) throw new IllegalArgumentException("Expected [x,y]");
        return new JSONArray().put(integer(value.get(0), 0, 1000))
                .put(integer(value.get(1), 0, 1000));
    }

    private static JSONObject phase(int duration, JSONArray path, JSONArray controls) throws Exception {
        JSONObject pointer = new JSONObject().put("id", 0).put("path", path);
        if (controls != null) pointer.put("control_points", controls);
        return new JSONObject().put("duration_ms", duration)
                .put("pointers", new JSONArray().put(pointer));
    }

    static JSONArray expand(JSONObject args, int longPressMs) throws Exception {
        for (Iterator<String> keys = args.keys(); keys.hasNext();)
            if (!FIELDS.contains(keys.next())) throw new IllegalArgumentException("Unknown long-press drag field");
        if (!"long_press_drag".equals(args.get("kind")))
            throw new IllegalArgumentException("Invalid long-press drag kind");
        JSONArray start = point(args.getJSONArray("start"));
        JSONArray end = point(args.getJSONArray("end"));
        JSONArray controls = null;
        if (args.has("control_points")) {
            JSONArray raw = args.getJSONArray("control_points");
            if (raw.length() != 2)
                throw new IllegalArgumentException("Cubic Bezier requires two control points");
            controls = new JSONArray().put(point(raw.getJSONArray(0))).put(point(raw.getJSONArray(1)));
        }
        int duration = integer(args.has("duration_ms") ? args.get("duration_ms") : 800, 1, 5000);
        // Do not silently shorten a device's configured long press to fit the budget.
        int hold = integer(args.has("hold_ms") ? args.get("hold_ms") : (long) longPressMs + 150, 1, 5000);
        int delay = integer(args.has("release_delay_ms") ? args.get("release_delay_ms") : 0, 0, 5000);

        JSONArray phases = new JSONArray()
                .put(phase(hold, new JSONArray().put(start), null))
                .put(phase(duration, new JSONArray().put(start).put(end), controls));
        // Zero lifts on the movement stroke; a positive delay holds at the chosen endpoint.
        if (delay > 0) phases.put(phase(delay, new JSONArray().put(end), null));
        return phases;
    }
}
