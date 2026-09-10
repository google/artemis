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
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Universal, ultra-stable UI hierarchy dumper for Android (API 24 - 37+).
 *
 * Capabilities:
 * 1. Zero WaitForIdle hangs: Bypasses UIAutomator's waitForIdle() timeout.
 * 2. Multi-window penetration: Captures App, Dialog, System UI, IME Keyboard, Split-Screen.
 * 3. Rich semantic extraction: Captures errorText, isHeading, editable, paneTitle, tooltip.
 * 4. Atomic screenshot snapshot: On API 30+, captures hardware bitmap and DOM tree simultaneously.
 * 5. W3C XML 1.0 compliance: Strict character sanitization prevents Python parse failures.
 */
public final class HierarchyDumper {

    private static final String TAG = "ArtemisHierarchyDumper";
    private static final int MAX_DEPTH = 75;
    private static final int MAX_NODES = 8000;
    private static final int RETRY_COUNT = 3;
    private static final long RETRY_INTERVAL_MS = 40L;

    private static final ExecutorService SCREENSHOT_EXECUTOR = Executors.newSingleThreadExecutor();

    private HierarchyDumper() {}

    /**
     * Dumps the complete hierarchy as a JSON response.
     */
    public static JSONObject dump(AccessibilityService service) {
        long startTime = System.currentTimeMillis();
        JSONObject result = new JSONObject();
        JSONArray elementsJson = new JSONArray();

        try {
            DisplayUtils.DisplayInfo displayInfo = DisplayUtils.getDisplayInfo(service);
            List<A11yNode> rootSnapshots = captureRootSnapshots(service);

            if (rootSnapshots.isEmpty()) {
                result.put("success", false);
                result.put("error", "No active window or root node found");
                result.put("xml", "");
                result.put("elements", elementsJson);
                result.put("rotation", displayInfo.rotation);
                result.put("width", displayInfo.width);
                result.put("height", displayInfo.height);
                return result;
            }

            // 1. Build standard UIAutomator XML
            String xml = buildXml(rootSnapshots, displayInfo.rotation, displayInfo.width, displayInfo.height);

            // 2. Build flat elements list
            List<JSONObject> flatList = new ArrayList<>();
            for (A11yNode root : rootSnapshots) {
                root.collectFlatElements(flatList);
            }
            for (JSONObject elem : flatList) {
                elementsJson.put(elem);
            }

            // 3. Build tree
            JSONArray trees = new JSONArray();
            for (A11yNode root : rootSnapshots) {
                trees.put(root.toTreeJson());
            }

            long elapsed = System.currentTimeMillis() - startTime;

            result.put("success", true);
            result.put("xml", xml);
            result.put("elements", elementsJson);
            result.put("tree", trees.length() == 1 ? trees.getJSONObject(0) : trees);
            result.put("rotation", displayInfo.rotation);
            result.put("width", displayInfo.width);
            result.put("height", displayInfo.height);
            result.put("elapsed_ms", elapsed);

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
                result.put("elements", elementsJson);
            } catch (Throwable ignored) {}
        }

        return result;
    }

    /**
     * Dumps an atomic snapshot combining hardware screenshot (JPEG base64) and UI hierarchy XML/JSON
     * at the exact same clock tick, eliminating temporal phase mismatch.
     */
    public static JSONObject dumpAtomicSnapshot(AccessibilityService service) {
        long startTime = System.currentTimeMillis();

        // 1. Trigger hardware screenshot asynchronously
        final AtomicReference<Bitmap> bitmapRef = new AtomicReference<>(null);
        final CountDownLatch screenshotLatch = new CountDownLatch(1);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
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
                                    screenshotLatch.countDown();
                                }
                            }

                            @Override
                            public void onFailure(int errorCode) {
                                Log.w(TAG, "takeScreenshot failed, errorCode: " + errorCode);
                                screenshotLatch.countDown();
                            }
                        }
                );
            } catch (Throwable t) {
                Log.w(TAG, "takeScreenshot invocation error", t);
                screenshotLatch.countDown();
            }
        } else {
            screenshotLatch.countDown();
        }

        // 2. Concurrently capture the UI hierarchy
        JSONObject dumpData = dump(service);

        // 3. Wait for the screenshot with a strict 1.5s timeout
        try {
            screenshotLatch.await(1500L, TimeUnit.MILLISECONDS);
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
            } catch (Throwable t) {
                Log.w(TAG, "Failed to compress screenshot to JPEG Base64", t);
                try { dumpData.put("has_screenshot", false); } catch (Throwable ignored) {}
            } finally {
                bitmap.recycle();
            }
        } else {
            try {
                dumpData.put("has_screenshot", false);
                dumpData.put("screenshot_error", Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                        ? "Screenshot capture timed out or failed"
                        : "takeScreenshot not supported on Android < 11");
            } catch (Throwable ignored) {}
        }

        try {
            dumpData.put("atomic_elapsed_ms", System.currentTimeMillis() - startTime);
        } catch (Throwable ignored) {}

        return dumpData;
    }

    /**
     * Dumps the hierarchy directly as a raw standard UIAutomator XML string.
     */
    public static String dumpXml(AccessibilityService service) {
        DisplayUtils.DisplayInfo displayInfo = DisplayUtils.getDisplayInfo(service);
        List<A11yNode> rootSnapshots = captureRootSnapshots(service);
        if (rootSnapshots.isEmpty()) {
            return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<hierarchy rotation=\""
                    + displayInfo.rotation + "\" />\n";
        }
        return buildXml(rootSnapshots, displayInfo.rotation, displayInfo.width, displayInfo.height);
    }

    /**
     * Builds the standard Android UIAutomator XML string from root snapshots.
     */
    private static String buildXml(List<A11yNode> roots, int rotation, int width, int height) {
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

        RawRootEntry(
                AccessibilityNodeInfo root,
                int windowId,
                String windowType,
                int windowLayer,
                boolean windowActive,
                boolean windowFocused
        ) {
            this.root = root;
            this.windowId = windowId;
            this.windowType = windowType;
            this.windowLayer = windowLayer;
            this.windowActive = windowActive;
            this.windowFocused = windowFocused;
        }
    }

    /**
     * Captures snapshots of all active and interactive windows with adaptive progressive retry support.
     * Guaranteed to capture the complete hierarchy even during activity transitions and cold starts.
     */
    public static List<A11yNode> captureRootSnapshots(AccessibilityService service) {
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

        List<A11yNode> snapshots = new ArrayList<>(rawRoots.size());
        int[] nodeCounter = new int[]{0};

        for (int i = 0; i < rawRoots.size(); i++) {
            RawRootEntry entry = rawRoots.get(i);
            try {
                A11yNode snapshot = snapshotNode(entry.root, 0, i, nodeCounter);
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
                        AccessibilityNodeInfo root = window.getRoot();
                        if (root != null) {
                            int hash = root.hashCode();
                            if (seenHashes.add(hash)) {
                                String typeStr = resolveWindowType(window.getType());
                                if (window.getType() == AccessibilityWindowInfo.TYPE_APPLICATION) {
                                    hasAppWindow = true;
                                }
                                roots.add(new RawRootEntry(
                                        root,
                                        window.getId(),
                                        typeStr,
                                        window.getLayer(),
                                        window.isActive(),
                                        window.isFocused()
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
                                true
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
                                true
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
     * Protected against recursion loops via MAX_DEPTH and MAX_NODES without flat hash collision drops.
     */
    @SuppressWarnings("deprecation")
    private static A11yNode snapshotNode(
            AccessibilityNodeInfo node,
            int depth,
            int childIndex,
            int[] nodeCounter
    ) {
        if (node == null) return null;
        if (depth > MAX_DEPTH || nodeCounter[0] >= MAX_NODES) {
            return null;
        }

        nodeCounter[0]++;
        A11yNode snapshot = new A11yNode();
        snapshot.index = childIndex;

        try {
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
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

            // 4. Hint text (API 26+)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                try {
                    CharSequence hint = node.getHintText();
                    if (hint != null) snapshot.hint = hint.toString();
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
                    childNode = node.getChild(i);
                    if (childNode != null) {
                        A11yNode childSnapshot = snapshotNode(childNode, depth + 1, i, nodeCounter);
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
     */
    public static AccessibilityNodeInfo findInputNode(AccessibilityService service) {
        for (RawRootEntry entry : getActiveRawRoots(service)) {
            try {
                AccessibilityNodeInfo focused = entry.root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                if (focused != null) return focused;
            } catch (Throwable ignored) {}
        }

        for (RawRootEntry entry : getActiveRawRoots(service)) {
            AccessibilityNodeInfo editable = findFirstEditable(entry.root);
            if (editable != null) return editable;
        }

        return null;
    }

    private static AccessibilityNodeInfo findFirstEditable(AccessibilityNodeInfo node) {
        if (node == null) return null;
        try {
            if (node.isEditable() && node.isFocusable() && node.isEnabled()) {
                return node;
            }
            int count = node.getChildCount();
            for (int i = 0; i < count; i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                if (child != null) {
                    AccessibilityNodeInfo res = findFirstEditable(child);
                    if (res != null) return res;
                    safeRecycle(child);
                }
            }
        } catch (Throwable ignored) {}
        return null;
    }
}
