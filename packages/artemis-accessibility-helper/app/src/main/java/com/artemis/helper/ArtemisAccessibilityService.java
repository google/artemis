package com.artemis.helper;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

/**
 * Artemis non-exclusive Accessibility Service with foreground keep-alive support.
 *
 * Runs concurrently with any Android UI testing framework (Mobly, Appium, Espresso)
 * without monopolizing Android's singleton UiAutomationService connection.
 */
public class ArtemisAccessibilityService extends AccessibilityService {

    private static final String TAG = "ArtemisA11yService";
    public static final int DEFAULT_PORT = 18888;
    private static final String CHANNEL_ID = "artemis_helper_channel";
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
            info.eventTypes = AccessibilityEvent.TYPES_ALL_MASK;
            info.feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC;
            info.notificationTimeout = 50;
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
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
                NotificationChannel channel = new NotificationChannel(
                        CHANNEL_ID,
                        "Artemis Helper Service",
                        NotificationManager.IMPORTANCE_LOW
                );
                channel.setDescription("Keeps Artemis Accessibility Helper active for automation testing");
                channel.setShowBadge(false);
                nm.createNotificationChannel(channel);
            }

            Notification.Builder builder;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder = new Notification.Builder(this, CHANNEL_ID);
            } else {
                builder = new Notification.Builder(this);
            }

            builder.setContentTitle("Artemis Accessibility Helper")
                    .setContentText("Active on 127.0.0.1:" + DEFAULT_PORT)
                    .setSmallIcon(android.R.drawable.stat_notify_sync)
                    .setOngoing(true);

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
