package com.artemis.helper;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * Receives the host's session token.
 *
 * The receiver is guarded in the manifest by
 * {@code android.permission.WRITE_SECURE_SETTINGS}, a permission ordinary apps
 * cannot hold but the adb shell user does, so only something with adb access to
 * the device can set (or clear) the token:
 *
 * <pre>
 * adb shell am broadcast -n com.artemis.helper/.TokenReceiver \
 *     -a com.artemis.helper.SET_TOKEN --es token &lt;hex&gt;
 * </pre>
 */
public class TokenReceiver extends BroadcastReceiver {

    private static final String TAG = "ArtemisTokenReceiver";
    public static final String ACTION_SET_TOKEN = "com.artemis.helper.SET_TOKEN";
    public static final String EXTRA_TOKEN = "token";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION_SET_TOKEN.equals(intent.getAction())) {
            return;
        }
        String value = intent.getStringExtra(EXTRA_TOKEN);
        TokenStore.set(value);
        Log.i(TAG, TokenStore.isSet() ? "Session token set" : "Session token cleared");
    }
}
