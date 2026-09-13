package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

/**
 * Artemis non-exclusive Accessibility Service with foreground keep-alive support.
 *
 * Runs concurrently with any Android UI testing framework (Mobly, Appium, Espresso)
 * without monopolizing Android's singleton UiAutomationService connection.
 *
 * The service subscribes to window-state changes only: that is the single event it
 * uses (to remember the foreground package / activity), and anything wider costs
 * CPU and battery on a device that is idle between tasks.
 */
public class ArtemisAccessibilityService extends AccessibilityService {

    private static final String TAG = "ArtemisA11yService";
    public static final int DEFAULT_PORT = 18888;
    /**
     * Bumped whenever the HTTP contract changes in a way an older host cannot use.
     * 2: token authentication, byte-accurate bodies, visible-only dumps, fields=.
     */
    public static final int PROTOCOL_VERSION = 2;
    // Channel importance is frozen by Android once a channel exists, so a quieter
    // channel needs a new id; the pre-1.1.3 channel is deleted on start.
    private static final String LEGACY_CHANNEL_ID = "artemis_helper_channel";
    private static final String CHANNEL_ID = "artemis_helper_quiet";
    private static final int NOTIFICATION_ID = 18888;

    private static volatile ArtemisAccessibilityService instance;

    private volatile String currentPackageName = "";
    private volatile String currentActivityName = "";
    private CommandServer server;

    public static ArtemisAccessibilityService getInstance() {
        return instance;
    }

    public String getCurrentPackageName() {
        return currentPackageName;
    }

    public String getCurrentActivityName() {
        return currentActivityName;
    }

    /** Installed helper versionCode, or -1 when the package manager cannot answer. */
    @SuppressWarnings("deprecation")
    public long getVersionCode() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                return info.getLongVersionCode();
            }
            return info.versionCode;
        } catch (Throwable t) {
            return -1L;
        }
    }

    public String getVersionName() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return info.versionName == null ? "" : info.versionName;
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        Log.i(TAG, "ArtemisAccessibilityService connected");

        // 1. Start foreground service to resist low-memory killer on aggressive custom ROMs
        startForegroundNotification();

        // 2. Dynamically enforce flags to guarantee compatibility across custom OEM ROMs
        try {
            AccessibilityServiceInfo info = getServiceInfo();
            if (info == null) {
                info = new AccessibilityServiceInfo();
            }
            info.eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED;
            info.feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC;
            info.notificationTimeout = 100;
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            // Not-important views are what UIAutomator's compressed dump leaves out;
            // keep them out here too so both backends describe the same tree.
            info.flags &= ~AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            setServiceInfo(info);
            Log.i(TAG, "AccessibilityServiceInfo flags dynamically enforced");
        } catch (Throwable t) {
            Log.w(TAG, "Failed to dynamically configure AccessibilityServiceInfo", t);
        }

        // 3. Start local loopback command server
        if (server != null) {
            server.shutdown();
            server = null;
        }

        server = new CommandServer(this, DEFAULT_PORT);
        server.setDaemon(true);
        server.start();
        Log.i(TAG, "CommandServer started on port " + DEFAULT_PORT);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @SuppressWarnings("deprecation")
    private void startForegroundNotification() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                try {
                    nm.deleteNotificationChannel(LEGACY_CHANNEL_ID);
                } catch (Throwable ignored) {}
                // Ask for IMPORTANCE_MIN (no sound, collapsed in the shade). Android raises
                // foreground-service channels to LOW at most, which is still silent. The
                // notification exists only to keep the service alive on aggressive ROMs;
                // it must not look like something the person needs to act on.
                NotificationChannel channel = new NotificationChannel(
                        CHANNEL_ID,
                        "Artemis test helper",
                        NotificationManager.IMPORTANCE_MIN
                );
                channel.setDescription("Shown while the Artemis test helper is installed on this device");
                channel.setShowBadge(false);
                nm.createNotificationChannel(channel);
            }

            Notification.Builder builder;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder = new Notification.Builder(this, CHANNEL_ID);
            } else {
                builder = new Notification.Builder(this);
            }

            builder.setContentTitle("Artemis test helper is running")
                    .setContentText("Lets Artemis read this screen during automated tests. "
                            + "Remove with: artemis helper uninstall")
                    .setStyle(new Notification.BigTextStyle().bigText(
                            "Installed by Artemis to read the screen layout during automated "
                            + "tests. It answers only on this device (127.0.0.1:" + DEFAULT_PORT
                            + "), only to the computer that is connected over USB debugging and "
                            + "holds the session token, and it sends nothing anywhere. Remove it "
                            + "any time with `artemis helper uninstall` or from Settings > Apps."))
                    .setSmallIcon(android.R.drawable.stat_notify_sync)
                    .setOngoing(true);
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
                builder.setPriority(Notification.PRIORITY_MIN);
            }

            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION_ID, builder.build(), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, builder.build(), 0);
            } else {
                startForeground(NOTIFICATION_ID, builder.build());
            }
            Log.i(TAG, "Foreground keep-alive notification active");
        } catch (Throwable t) {
            Log.w(TAG, "Foreground notification start skipped or deferred: " + t.getMessage());
        }
    }

    @SuppressWarnings("deprecation")
    private void stopForegroundNotification() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE);
            } else {
                stopForeground(true);
            }
        } catch (Throwable ignored) {}
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) return;
        if (event.getEventType() == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            CharSequence pkg = event.getPackageName();
            CharSequence cls = event.getClassName();
            if (pkg != null) currentPackageName = pkg.toString();
            if (cls != null) currentActivityName = cls.toString();
        }
    }

    @Override
    public void onInterrupt() {
        Log.w(TAG, "ArtemisAccessibilityService interrupted");
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        Log.i(TAG, "ArtemisAccessibilityService destroyed");
        stopForegroundNotification();
        if (server != null) {
            server.shutdown();
            server = null;
        }
        if (instance == this) {
            instance = null;
        }
    }
}
