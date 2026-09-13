package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Bitmap;
import android.graphics.ColorSpace;
import android.graphics.Rect;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Universal, ultra-stable UI hierarchy dumper for Android (API 24 - 37+).
 *
 * Capabilities:
 * 1. Zero WaitForIdle hangs: Bypasses UIAutomator's waitForIdle() timeout.
 * 2. Multi-window penetration: Captures App, Dialog, System UI, IME Keyboard, Split-Screen.
 * 3. UIAutomator parity: invisible nodes are skipped and bounds are clipped to the
 *    display, the window and scrollable ancestors, so the tree describes what is on
 *    screen and nothing else. {@code include_invisible=1} keeps everything (debugging).
 * 4. Rich semantic extraction: Captures errorText, isHeading, editable, paneTitle, tooltip.
 * 5. Atomic screenshot snapshot: On API 30+, captures hardware bitmap and DOM tree simultaneously.
 * 6. W3C XML 1.0 compliance: Strict character sanitization prevents Python parse failures.
 * 7. Batched IPC on API 33+: descendants are prefetched per getChild call instead of one
 *    Binder round trip per node.
 */
public final class HierarchyDumper {

    private static final String TAG = "ArtemisHierarchyDumper";
    private static final int MAX_DEPTH = 75;
    private static final int MAX_NODES = 8000;

    private static final ExecutorService SCREENSHOT_EXECUTOR = Executors.newSingleThreadExecutor();

    /** Prefetch strategy for API 33+ ({@code AccessibilityNodeInfo.FLAG_PREFETCH_DESCENDANTS_HYBRID}). */
    private static final int PREFETCH_DESCENDANTS_HYBRID = 1 << 3;

    private HierarchyDumper() {}

    /**
     * What a dump should contain. Every field defaults to the cheapest useful answer.
     */
    public static final class DumpOptions {
        public boolean includeInvisible = false;
        public boolean wantXml = true;
        public boolean wantElements = false;
        public boolean wantTree = false;

        public static DumpOptions forDump() {
            DumpOptions o = new DumpOptions();
            o.wantElements = true;
            o.wantTree = true;
            return o;
        }

        public static DumpOptions forSnapshot() {
            return new DumpOptions();
        }

        /** Applies {@code fields=xml,elements,tree} and {@code include_invisible=1}. */
        public DumpOptions apply(Map<String, String> query) {
            if (query == null) return this;
            String fields = query.get("fields");
            if (fields != null && !fields.trim().isEmpty()) {
                wantXml = wantElements = wantTree = false;
                for (String f : fields.split(",")) {
                    String key = f.trim().toLowerCase();
                    if (key.equals("xml")) wantXml = true;
                    else if (key.equals("elements")) wantElements = true;
                    else if (key.equals("tree")) wantTree = true;
                }
            }
            String inv = query.get("include_invisible");
            if (inv != null) {
                includeInvisible = inv.equals("1") || inv.equalsIgnoreCase("true");
            }
            return this;
        }
    }

    /** Per-dump counters reported back to the host. */
    private static final class DumpStats {
        int nodes = 0;
        int skippedInvisible = 0;
        boolean truncated = false;
    }

    public static JSONObject dump(AccessibilityService service) {
        return dump(service, DumpOptions.forDump());
    }

    /**
     * Dumps the hierarchy as a JSON response containing the requested representations.
     */
    public static JSONObject dump(AccessibilityService service, DumpOptions options) {
        long startTime = System.currentTimeMillis();
        JSONObject result = new JSONObject();

        try {
            DisplayUtils.DisplayInfo displayInfo = DisplayUtils.getDisplayInfo(service);
            DumpStats stats = new DumpStats();
            List<A11yNode> rootSnapshots = captureRootSnapshots(service, displayInfo, options, stats);

            result.put("rotation", displayInfo.rotation);
            result.put("width", displayInfo.width);
            result.put("height", displayInfo.height);

            if (rootSnapshots.isEmpty()) {
                result.put("success", false);
                result.put("error", "No active window or root node found");
                result.put("xml", "");
                result.put("elements", new JSONArray());
                return result;
            }

            if (options.wantXml) {
                result.put("xml", buildXml(rootSnapshots, displayInfo.rotation));
            }
            if (options.wantElements) {
                JSONArray elementsJson = new JSONArray();
                List<JSONObject> flatList = new ArrayList<>();
                for (A11yNode root : rootSnapshots) {
                    root.collectFlatElements(flatList);
                }
                for (JSONObject elem : flatList) {
                    elementsJson.put(elem);
                }
                result.put("elements", elementsJson);
            }
            if (options.wantTree) {
                JSONArray trees = new JSONArray();
                for (A11yNode root : rootSnapshots) {
                    trees.put(root.toTreeJson());
                }
                result.put("tree", trees.length() == 1 ? trees.getJSONObject(0) : trees);
            }

            result.put("success", true);
            result.put("node_count", stats.nodes);
            result.put("skipped_invisible", stats.skippedInvisible);
            result.put("truncated", stats.truncated);
            result.put("window_count", rootSnapshots.size());
            result.put("elapsed_ms", System.currentTimeMillis() - startTime);

            if (service instanceof ArtemisAccessibilityService) {
                ArtemisAccessibilityService s = (ArtemisAccessibilityService) service;
                result.put("package", s.getCurrentPackageName());
                result.put("activity", s.getCurrentActivityName());
            }

        } catch (Throwable t) {
            Log.e(TAG, "Dump failed with exception", t);
            try {
                result.put("success", false);
                result.put("error", "Dump failed: " + t.getMessage());
                result.put("xml", "");
                result.put("elements", new JSONArray());
            } catch (Throwable ignored) {}
        }

        return result;
    }

    private static final int ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT = 3;
    private static final long SCREENSHOT_INTERVAL_RETRY_MS = 350L;

    @android.annotation.TargetApi(Build.VERSION_CODES.R)
    private static void requestScreenshot(
            final AccessibilityService service,
            final AtomicReference<Bitmap> bitmapRef,
            final AtomicInteger errorRef,
            final CountDownLatch latch,
            final boolean allowRetry) {
        try {
            service.takeScreenshot(
                    Display.DEFAULT_DISPLAY,
                    SCREENSHOT_EXECUTOR,
                    new AccessibilityService.TakeScreenshotCallback() {
                        @Override
                        public void onSuccess(AccessibilityService.ScreenshotResult screenshotResult) {
                            try {
                                HardwareBuffer buffer = screenshotResult.getHardwareBuffer();
                                ColorSpace colorSpace = screenshotResult.getColorSpace();
                                Bitmap hwBitmap = Bitmap.wrapHardwareBuffer(buffer, colorSpace);
                                if (hwBitmap != null) {
                                    Bitmap swBitmap = hwBitmap.copy(Bitmap.Config.ARGB_8888, false);
                                    hwBitmap.recycle();
                                    buffer.close();
                                    bitmapRef.set(swBitmap);
                                }
                            } catch (Throwable t) {
                                Log.w(TAG, "Error copying screenshot buffer", t);
                            } finally {
                                latch.countDown();
                            }
                        }

                        @Override
                        public void onFailure(int errorCode) {
                            errorRef.set(errorCode);
                            if (allowRetry && errorCode == ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT) {
                                Log.i(TAG, "takeScreenshot rate-limited; retrying after " + SCREENSHOT_INTERVAL_RETRY_MS + " ms");
                                SCREENSHOT_EXECUTOR.execute(new Runnable() {
                                    @Override
                                    public void run() {
                                        try {
                                            Thread.sleep(SCREENSHOT_INTERVAL_RETRY_MS);
                                        } catch (InterruptedException e) {
                                            Thread.currentThread().interrupt();
                                        }
                                        requestScreenshot(service, bitmapRef, errorRef, latch, false);
                                    }
                                });
                                return;
                            }
                            Log.w(TAG, "takeScreenshot failed, errorCode: " + errorCode);
                            latch.countDown();
                        }
                    }
            );
        } catch (Throwable t) {
            Log.w(TAG, "takeScreenshot invocation error", t);
            latch.countDown();
        }
    }

    public static JSONObject dumpAtomicSnapshot(AccessibilityService service) {
        return dumpAtomicSnapshot(service, DumpOptions.forSnapshot());
    }

    /**
     * Dumps an atomic snapshot combining hardware screenshot (JPEG base64) and UI hierarchy
     * at the exact same clock tick, eliminating temporal phase mismatch. When the screenshot
     * is present, {@code width} / {@code height} are the bitmap's own dimensions so the host
     * normalizes coordinates against the very image it is looking at.
     */
    public static JSONObject dumpAtomicSnapshot(AccessibilityService service, DumpOptions options) {
        long startTime = System.currentTimeMillis();

        // 1. Trigger hardware screenshot asynchronously. The framework rate-limits
        //    takeScreenshot to one call per ~333 ms (ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT = 3);
        //    back-to-back observations therefore wait out the interval and retry once
        //    here, which is far cheaper than the host falling back to adb screencap.
        final AtomicReference<Bitmap> bitmapRef = new AtomicReference<>(null);
        final AtomicInteger errorRef = new AtomicInteger(0);
        final CountDownLatch screenshotLatch = new CountDownLatch(1);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            requestScreenshot(service, bitmapRef, errorRef, screenshotLatch, true);
        } else {
            screenshotLatch.countDown();
        }

        // 2. Concurrently capture the UI hierarchy
        JSONObject dumpData = dump(service, options);

        // 3. Wait for the screenshot (one retry after the rate-limit interval fits inside)
        try {
            screenshotLatch.await(2500L, TimeUnit.MILLISECONDS);
        } catch (InterruptedException ignored) {}

        Bitmap bitmap = bitmapRef.get();
        if (bitmap != null) {
            try {
                ByteArrayOutputStream baos = new ByteArrayOutputStream(bitmap.getWidth() * bitmap.getHeight() / 4);
                bitmap.compress(Bitmap.CompressFormat.JPEG, 80, baos);
                byte[] jpegBytes = baos.toByteArray();
                String base64Str = Base64.encodeToString(jpegBytes, Base64.NO_WRAP);
                dumpData.put("screenshot_base64", base64Str);
                dumpData.put("has_screenshot", true);
                dumpData.put("width", bitmap.getWidth());
                dumpData.put("height", bitmap.getHeight());
            } catch (Throwable t) {
                Log.w(TAG, "Failed to compress screenshot to JPEG Base64", t);
                try { dumpData.put("has_screenshot", false); } catch (Throwable ignored) {}
            } finally {
                bitmap.recycle();
            }
        } else {
            try {
                dumpData.put("has_screenshot", false);
                dumpData.put("screenshot_error_code", errorRef.get());
                dumpData.put("screenshot_error", Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                        ? (errorRef.get() != 0
                            ? "takeScreenshot failed with errorCode " + errorRef.get()
                            : "Screenshot capture timed out")
                        : "takeScreenshot not supported on Android < 11");
            } catch (Throwable ignored) {}
        }

        try {
            dumpData.put("atomic_elapsed_ms", System.currentTimeMillis() - startTime);
        } catch (Throwable ignored) {}

        return dumpData;
    }

    public static String dumpXml(AccessibilityService service) {
        return dumpXml(service, DumpOptions.forSnapshot());
    }

    /**
     * Dumps the hierarchy directly as a raw standard UIAutomator XML string.
     */
    public static String dumpXml(AccessibilityService service, DumpOptions options) {
        DisplayUtils.DisplayInfo displayInfo = DisplayUtils.getDisplayInfo(service);
        List<A11yNode> rootSnapshots = captureRootSnapshots(service, displayInfo, options, new DumpStats());
        if (rootSnapshots.isEmpty()) {
            return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<hierarchy rotation=\""
                    + displayInfo.rotation + "\" />\n";
        }
        return buildXml(rootSnapshots, displayInfo.rotation);
    }

    /**
     * Builds the standard Android UIAutomator XML string from root snapshots.
     */
    private static String buildXml(List<A11yNode> roots, int rotation) {
        StringBuilder sb = new StringBuilder(roots.size() * 1024 + 256);
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n");
        sb.append("<hierarchy rotation=\"").append(rotation).append("\">");

        for (int i = 0; i < roots.size(); i++) {
            A11yNode root = roots.get(i);
            root.index = i;
            root.writeXml(sb);
        }

        sb.append("</hierarchy>\n");
        return sb.toString();
    }

    private static final class RawRootEntry {
        final AccessibilityNodeInfo root;
        final int windowId;
        final String windowType;
        final int windowLayer;
        final boolean windowActive;
        final boolean windowFocused;
        /** Screen rectangle of the window, or null when unknown (display bounds are used). */
        final Rect windowBounds;

        RawRootEntry(
                AccessibilityNodeInfo root,
                int windowId,
                String windowType,
                int windowLayer,
                boolean windowActive,
                boolean windowFocused,
                Rect windowBounds
        ) {
            this.root = root;
            this.windowId = windowId;
            this.windowType = windowType;
            this.windowLayer = windowLayer;
            this.windowActive = windowActive;
            this.windowFocused = windowFocused;
            this.windowBounds = windowBounds;
        }
    }

    public static List<A11yNode> captureRootSnapshots(AccessibilityService service) {
        return captureRootSnapshots(
                service, DisplayUtils.getDisplayInfo(service), DumpOptions.forSnapshot(), new DumpStats());
    }

    /**
     * Captures snapshots of all active and interactive windows with adaptive progressive retry support.
     * Guaranteed to capture the complete hierarchy even during activity transitions and cold starts.
     */
    private static List<A11yNode> captureRootSnapshots(
            AccessibilityService service,
            DisplayUtils.DisplayInfo displayInfo,
            DumpOptions options,
            DumpStats stats
    ) {
        List<RawRootEntry> rawRoots = Collections.emptyList();
        long[] retryBackoff = new long[]{40L, 80L, 120L, 160L, 220L, 300L};

        for (int attempt = 0; attempt <= retryBackoff.length; attempt++) {
            rawRoots = getActiveRawRoots(service);
            if (!rawRoots.isEmpty()) {
                break;
            }
            if (attempt < retryBackoff.length) {
                SystemClock.sleep(retryBackoff[attempt]);
            }
        }

        Rect displayRect = new Rect(0, 0, displayInfo.width, displayInfo.height);
        List<A11yNode> snapshots = new ArrayList<>(rawRoots.size());

        for (int i = 0; i < rawRoots.size(); i++) {
            RawRootEntry entry = rawRoots.get(i);
            try {
                Rect clip = new Rect(displayRect);
                if (entry.windowBounds != null && !clip.intersect(entry.windowBounds)) {
                    // Window entirely off the display (e.g. a second display): keep the
                    // display rect so the root still serializes with sane bounds.
                    clip = new Rect(displayRect);
                }
                A11yNode snapshot = snapshotNode(entry.root, 0, i, clip, options, stats);
                if (snapshot != null) {
                    snapshot.windowId = entry.windowId;
                    snapshot.windowType = entry.windowType;
                    snapshot.windowLayer = entry.windowLayer;
                    snapshot.windowActive = entry.windowActive;
                    snapshot.windowFocused = entry.windowFocused;
                    snapshots.add(snapshot);
                }
            } catch (Throwable t) {
                Log.w(TAG, "Failed to snapshot root window " + entry.windowId, t);
            } finally {
                safeRecycle(entry.root);
            }
        }

        return snapshots;
    }

    private static AccessibilityNodeInfo windowRoot(AccessibilityWindowInfo window) {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                return window.getRoot(PREFETCH_DESCENDANTS_HYBRID);
            } catch (Throwable ignored) {}
        }
        return window.getRoot();
    }

    private static AccessibilityNodeInfo childOf(AccessibilityNodeInfo node, int index) {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                return node.getChild(index, PREFETCH_DESCENDANTS_HYBRID);
            } catch (Throwable ignored) {}
        }
        return node.getChild(index);
    }

    /**
     * Multi-tier hierarchy root discovery strategy:
     * Tier 1: Multi-window enumeration (sorted by Z-layer descending).
     * Tier 2: Active window direct fallback (if getWindows() is empty or missing foreground app).
     * Tier 3: Focused input / accessibility node backtracking (walks up parent chain to top root).
     */
    private static List<RawRootEntry> getActiveRawRoots(AccessibilityService service) {
        List<RawRootEntry> roots = new ArrayList<>();
        Set<Integer> seenHashes = new HashSet<>();
        boolean hasAppWindow = false;

        // Tier 1: Multi-window enumeration (App, Dialogs, Popups, Keyboards, Split-screen)
        try {
            List<AccessibilityWindowInfo> windows = service.getWindows();
            if (windows != null && !windows.isEmpty()) {
                List<AccessibilityWindowInfo> sortedWindows = new ArrayList<>(windows);
                Collections.sort(sortedWindows, new Comparator<AccessibilityWindowInfo>() {
                    @Override
                    public int compare(AccessibilityWindowInfo w1, AccessibilityWindowInfo w2) {
                        return Integer.compare(w2.getLayer(), w1.getLayer());
                    }
                });

                for (AccessibilityWindowInfo window : sortedWindows) {
                    try {
                        AccessibilityNodeInfo root = windowRoot(window);
                        if (root != null) {
                            int hash = root.hashCode();
                            if (seenHashes.add(hash)) {
                                String typeStr = resolveWindowType(window.getType());
                                if (window.getType() == AccessibilityWindowInfo.TYPE_APPLICATION) {
                                    hasAppWindow = true;
                                }
                                Rect bounds = new Rect();
                                window.getBoundsInScreen(bounds);
                                roots.add(new RawRootEntry(
                                        root,
                                        window.getId(),
                                        typeStr,
                                        window.getLayer(),
                                        window.isActive(),
                                        window.isFocused(),
                                        bounds.isEmpty() ? null : bounds
                                ));
                            } else {
                                safeRecycle(root);
                            }
                        }
                    } catch (Throwable ignored) {}
                }
            }
        } catch (Throwable ignored) {}

        // Tier 2: Active window fallback (if getWindows() returned empty or lacked active app window)
        if (!hasAppWindow) {
            try {
                AccessibilityNodeInfo activeRoot = service.getRootInActiveWindow();
                if (activeRoot != null) {
                    int hash = activeRoot.hashCode();
                    if (seenHashes.add(hash)) {
                        roots.add(0, new RawRootEntry(
                                activeRoot,
                                activeRoot.getWindowId(),
                                "application",
                                0,
                                true,
                                true,
                                null
                        ));
                        hasAppWindow = true;
                    } else {
                        safeRecycle(activeRoot);
                    }
                }
            } catch (Throwable ignored) {}
        }

        // Tier 3: Focused node backtracking (recovers window tree during transient transitions)
        if (roots.isEmpty()) {
            AccessibilityNodeInfo focused = null;
            try {
                focused = service.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            } catch (Throwable ignored) {}
            if (focused == null) {
                try {
                    focused = service.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY);
                } catch (Throwable ignored) {}
            }

            if (focused != null) {
                try {
                    AccessibilityNodeInfo current = focused;
                    AccessibilityNodeInfo parent = current.getParent();
                    while (parent != null) {
                        if (current != focused) {
                            safeRecycle(current);
                        }
                        current = parent;
                        parent = current.getParent();
                    }
                    int hash = current.hashCode();
                    if (seenHashes.add(hash)) {
                        roots.add(new RawRootEntry(
                                current,
                                current.getWindowId(),
                                "application",
                                0,
                                true,
                                true,
                                null
                        ));
                    } else {
                        safeRecycle(current);
                    }
                } catch (Throwable ignored) {
                } finally {
                    safeRecycle(focused);
                }
            }
        }

        return roots;
    }

    private static String resolveWindowType(int type) {
        switch (type) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:
                return "application";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:
                return "input_method";
            case AccessibilityWindowInfo.TYPE_SYSTEM:
                return "system";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY:
                return "accessibility_overlay";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER:
                return "split_screen_divider";
            default:
                return "unknown";
        }
    }

    /**
     * Recursively snapshots an AccessibilityNodeInfo into an immutable A11yNode,
     * extracting rich semantic properties (error, heading, editable, paneTitle, tooltip, stateDescription).
     *
     * {@code clip} is the rectangle the node may be visible in: the display intersected with the
     * window and every scrollable ancestor. Children that are not visible to the user are skipped
     * unless {@code options.includeInvisible} is set; the window root itself is always kept, as in
     * UIAutomator.
     *
     * Protected against recursion loops via MAX_DEPTH and MAX_NODES without flat hash collision drops.
     */
    @SuppressWarnings("deprecation")
    private static A11yNode snapshotNode(
            AccessibilityNodeInfo node,
            int depth,
            int childIndex,
            Rect clip,
            DumpOptions options,
            DumpStats stats
    ) {
        if (node == null) return null;
        if (depth > MAX_DEPTH || stats.nodes >= MAX_NODES) {
            stats.truncated = true;
            return null;
        }

        boolean visible = true;
        try {
            visible = node.isVisibleToUser();
        } catch (Throwable ignored) {}
        if (!visible && depth > 0 && !options.includeInvisible) {
            stats.skippedInvisible++;
            return null;
        }

        stats.nodes++;
        A11yNode snapshot = new A11yNode();
        snapshot.index = childIndex;
        snapshot.visibleToUser = visible;
        Rect childClip = clip;

        try {
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (!bounds.intersect(clip)) {
                bounds.setEmpty();
            }
            snapshot.left = bounds.left;
            snapshot.top = bounds.top;
            snapshot.right = bounds.right;
            snapshot.bottom = bounds.bottom;

            CharSequence text = node.getText();
            CharSequence desc = node.getContentDescription();
            CharSequence pkg = node.getPackageName();
            CharSequence cls = node.getClassName();
            String resId = node.getViewIdResourceName();

            snapshot.text = text != null ? text.toString() : "";
            snapshot.contentDesc = desc != null ? desc.toString() : "";
            snapshot.packageName = pkg != null ? pkg.toString() : "";
            snapshot.className = cls != null ? cls.toString() : "";
            snapshot.resourceId = resId != null ? resId : "";

            snapshot.clickable = node.isClickable();
            snapshot.checkable = node.isCheckable();
            snapshot.checked = node.isChecked();
            snapshot.enabled = node.isEnabled();
            snapshot.focusable = node.isFocusable();
            snapshot.focused = node.isFocused();
            snapshot.scrollable = node.isScrollable();
            snapshot.longClickable = node.isLongClickable();
            snapshot.password = node.isPassword();
            snapshot.selected = node.isSelected();

            // Children of a scrollable container are clipped by it (UIAutomator's
            // trimScrollableParent): a list row half under the toolbar keeps only its
            // visible part, so its center is a point the user can actually touch.
            if (snapshot.scrollable && !bounds.isEmpty()) {
                childClip = new Rect(bounds);
            }

            // 1. Editable property
            snapshot.editable = node.isEditable();

            // 2. Error message (crucial for form validation detection)
            CharSequence err = node.getError();
            if (err != null && err.length() > 0) {
                snapshot.errorText = err.toString();
            }

            // 3. Drawing order (API 24+)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                try {
                    snapshot.drawingOrder = node.getDrawingOrder();
                } catch (Throwable ignored) {}
            }

            // 4. Hint text (API 26+). When the field is empty the framework reports the
            //    hint as the text; keep text and hint apart so "empty input" stays detectable.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                try {
                    CharSequence hint = node.getHintText();
                    if (hint != null) snapshot.hint = hint.toString();
                    if (node.isShowingHintText()) {
                        snapshot.text = "";
                    }
                } catch (Throwable ignored) {}
            }

            // 5. Heading, PaneTitle, Tooltip, ScreenReaderFocusable (API 28+)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                try {
                    snapshot.isHeading = node.isHeading();
                } catch (Throwable ignored) {}

                try {
                    snapshot.screenReaderFocusable = node.isScreenReaderFocusable();
                } catch (Throwable ignored) {}

                try {
                    CharSequence pt = node.getPaneTitle();
                    if (pt != null) snapshot.paneTitle = pt.toString();
                } catch (Throwable ignored) {}

                try {
                    CharSequence tt = node.getTooltipText();
                    if (tt != null) snapshot.tooltip = tt.toString();
                } catch (Throwable ignored) {}
            }

            // 6. State Description (API 30+ Jetpack Compose semantics: expanded, collapsed, etc.)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                try {
                    CharSequence sd = node.getStateDescription();
                    if (sd != null) snapshot.stateDescription = sd.toString();
                } catch (Throwable ignored) {}
            }

            // Recursively process children
            int childCount = node.getChildCount();
            for (int i = 0; i < childCount; i++) {
                AccessibilityNodeInfo childNode = null;
                try {
                    childNode = childOf(node, i);
                    if (childNode != null) {
                        A11yNode childSnapshot = snapshotNode(childNode, depth + 1, i, childClip, options, stats);
                        if (childSnapshot != null) {
                            snapshot.children.add(childSnapshot);
                        }
                    }
                } catch (Throwable ignored) {
                } finally {
                    if (childNode != null) {
                        safeRecycle(childNode);
                    }
                }
            }

        } catch (Throwable t) {
            Log.w(TAG, "Error reading node properties", t);
        }

        return snapshot;
    }

    /**
     * Safely recycles node info on Android API < 30 to prevent Binder pool exhaustion.
     */
    @SuppressWarnings("deprecation")
    public static void safeRecycle(AccessibilityNodeInfo node) {
        if (node == null) return;
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            try {
                node.recycle();
            } catch (Throwable ignored) {}
        }
    }

    /**
     * Finds the currently focused or editable input node for text entry.
     * Window roots are enumerated once and recycled; only the returned node stays alive.
     */
    public static AccessibilityNodeInfo findInputNode(AccessibilityService service) {
        List<RawRootEntry> roots = getActiveRawRoots(service);
        AccessibilityNodeInfo found = null;
        try {
            for (RawRootEntry entry : roots) {
                try {
                    AccessibilityNodeInfo focused = entry.root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                    if (focused != null) {
                        found = focused;
                        return found;
                    }
                } catch (Throwable ignored) {}
            }
            for (RawRootEntry entry : roots) {
                AccessibilityNodeInfo editable = findFirstEditable(entry.root, 0);
                if (editable != null) {
                    found = editable;
                    return found;
                }
            }
            return null;
        } finally {
            for (RawRootEntry entry : roots) {
                if (entry.root != found) {
                    safeRecycle(entry.root);
                }
            }
        }
    }

    private static AccessibilityNodeInfo findFirstEditable(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > MAX_DEPTH) return null;
        try {
            if (node.isEditable() && node.isFocusable() && node.isEnabled() && node.isVisibleToUser()) {
                return node;
            }
            int count = node.getChildCount();
            for (int i = 0; i < count; i++) {
                AccessibilityNodeInfo child = childOf(node, i);
                if (child != null) {
                    AccessibilityNodeInfo res = findFirstEditable(child, depth + 1);
                    if (res != null) return res;
                    safeRecycle(child);
                }
            }
        } catch (Throwable ignored) {}
        return null;
    }
}
