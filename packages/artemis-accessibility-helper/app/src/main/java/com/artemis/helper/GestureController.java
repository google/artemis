package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Synchronous gesture and action executor using native AccessibilityService APIs.
 * Works without requiring UiAutomation or root privileges.
 */
public final class GestureController {

    private static final String TAG = "ArtemisGestureCtrl";
    private static final Handler MAIN_HANDLER = new Handler(Looper.getMainLooper());

    private GestureController() {}

    public static boolean tap(AccessibilityService service, float x, float y, long timeoutMs) {
        Path path = new Path();
        path.moveTo(x, y);
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(path, 0L, 60L);
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        return dispatchSynchronous(service, gesture, timeoutMs);
    }

    public static boolean doubleTap(AccessibilityService service, float x, float y, long timeoutMs) {
        if (!tap(service, x, y, timeoutMs)) return false;
        try {
            Thread.sleep(100L);
        } catch (InterruptedException ignored) {}
        return tap(service, x, y, timeoutMs);
    }

    public static boolean longPress(AccessibilityService service, float x, float y, long durationMs, long timeoutMs) {
        Path path = new Path();
        path.moveTo(x, y);
        long pressDur = Math.max(500L, Math.min(durationMs, 5000L));
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(path, 0L, pressDur);
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        return dispatchSynchronous(service, gesture, timeoutMs + pressDur);
    }

    public static boolean swipe(
            AccessibilityService service,
            float x1, float y1, float x2, float y2,
            long durationMs, long timeoutMs
    ) {
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        long safeDuration = Math.max(50L, Math.min(durationMs, 5000L));
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(path, 0L, safeDuration);
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        return dispatchSynchronous(service, gesture, timeoutMs + safeDuration);
    }

    public static boolean setText(AccessibilityService service, String text) {
        AccessibilityNodeInfo inputNode = HierarchyDumper.findInputNode(service);
        if (inputNode == null) {
            Log.w(TAG, "No editable/focused input node found for setText");
            return false;
        }
        try {
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return inputNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        } catch (Throwable t) {
            Log.w(TAG, "performAction ACTION_SET_TEXT failed", t);
            return false;
        } finally {
            HierarchyDumper.safeRecycle(inputNode);
        }
    }

    public static boolean clearText(AccessibilityService service) {
        return setText(service, "");
    }

    public static boolean performGlobalAction(AccessibilityService service, String actionName) {
        if (actionName == null) return false;
        int actionId;
        String lower = actionName.toLowerCase();
        switch (lower) {
            case "back":
                actionId = AccessibilityService.GLOBAL_ACTION_BACK;
                break;
            case "home":
                actionId = AccessibilityService.GLOBAL_ACTION_HOME;
                break;
            case "recents":
                actionId = AccessibilityService.GLOBAL_ACTION_RECENTS;
                break;
            case "notifications":
                actionId = AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS;
                break;
            case "quick_settings":
                actionId = AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS;
                break;
            case "power_dialog":
                actionId = AccessibilityService.GLOBAL_ACTION_POWER_DIALOG;
                break;
            case "toggle_split_screen":
                actionId = AccessibilityService.GLOBAL_ACTION_TOGGLE_SPLIT_SCREEN;
                break;
            case "lock_screen":
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    actionId = AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN;
                } else {
                    return false;
                }
                break;
            case "take_screenshot":
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    actionId = AccessibilityService.GLOBAL_ACTION_TAKE_SCREENSHOT;
                } else {
                    return false;
                }
                break;
            default:
                return false;
        }
        return service.performGlobalAction(actionId);
    }

    private static boolean dispatchSynchronous(
            final AccessibilityService service,
            final GestureDescription gesture,
            long timeoutMs
    ) {
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicBoolean result = new AtomicBoolean(false);

        MAIN_HANDLER.post(new Runnable() {
            @Override
            public void run() {
                try {
                    service.dispatchGesture(gesture, new AccessibilityService.GestureResultCallback() {
                        @Override
                        public void onCompleted(GestureDescription gestureDescription) {
                            result.set(true);
                            latch.countDown();
                        }

                        @Override
                        public void onCancelled(GestureDescription gestureDescription) {
                            result.set(false);
                            latch.countDown();
                        }
                    }, null);
                } catch (Throwable t) {
                    Log.w(TAG, "dispatchGesture threw exception", t);
                    result.set(false);
                    latch.countDown();
                }
            }
        });

        try {
            return latch.await(timeoutMs, TimeUnit.MILLISECONDS) && result.get();
        } catch (InterruptedException e) {
            return false;
        }
    }
}
