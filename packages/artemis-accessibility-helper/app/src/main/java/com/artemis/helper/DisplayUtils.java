package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.content.Context;
import android.graphics.Point;
import android.os.Build;
import android.util.DisplayMetrics;
import android.view.Display;
import android.view.Surface;
import android.view.WindowManager;

/**
 * Universal display and orientation utilities compatible across all Android versions (API 24 - 37+).
 */
public final class DisplayUtils {

    private DisplayUtils() {}

    public static class DisplayInfo {
        public final int rotation;
        public final int width;
        public final int height;

        public DisplayInfo(int rotation, int width, int height) {
            this.rotation = rotation;
            this.width = width;
            this.height = height;
        }
    }

    /**
     * Obtains the display orientation (0, 1, 2, 3) and screen dimensions (width, height)
     * using the most reliable API for the current Android runtime.
     */
    @SuppressWarnings("deprecation")
    public static DisplayInfo getDisplayInfo(AccessibilityService service) {
        int rotation = 0;
        int width = 1080;
        int height = 2400;

        // Baseline initialization from resources metrics (handles tablets, TVs, emulators dynamically)
        try {
            DisplayMetrics resDm = service.getResources().getDisplayMetrics();
            if (resDm != null && resDm.widthPixels > 0 && resDm.heightPixels > 0) {
                width = resDm.widthPixels;
                height = resDm.heightPixels;
            }
        } catch (Throwable ignored) {}

        try {
            // Modern API 30+ window metrics for full physical screen dimensions
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                try {
                    WindowManager wm = (WindowManager) service.getSystemService(Context.WINDOW_SERVICE);
                    if (wm != null) {
                        android.graphics.Rect bounds = wm.getMaximumWindowMetrics().getBounds();
                        if (bounds.width() > 0 && bounds.height() > 0) {
                            width = bounds.width();
                            height = bounds.height();
                        }
                    }
                } catch (Throwable ignored) {}
            }

            // Resolve Display instance for rotation and legacy metrics
            Display display = null;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                try {
                    display = service.getDisplay();
                } catch (Throwable ignored) {}
            }

            if (display == null) {
                WindowManager wm = (WindowManager) service.getSystemService(Context.WINDOW_SERVICE);
                if (wm != null) {
                    display = wm.getDefaultDisplay();
                }
            }

            if (display != null) {
                int r = display.getRotation();
                switch (r) {
                    case Surface.ROTATION_0:
                        rotation = 0;
                        break;
                    case Surface.ROTATION_90:
                        rotation = 1;
                        break;
                    case Surface.ROTATION_180:
                        rotation = 2;
                        break;
                    case Surface.ROTATION_270:
                        rotation = 3;
                        break;
                    default:
                        rotation = 0;
                        break;
                }

                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
                    DisplayMetrics dm = new DisplayMetrics();
                    display.getRealMetrics(dm);
                    width = dm.widthPixels;
                    height = dm.heightPixels;
                }
            }
        } catch (Throwable ignored) {}

        return new DisplayInfo(rotation, width, height);
    }
}
