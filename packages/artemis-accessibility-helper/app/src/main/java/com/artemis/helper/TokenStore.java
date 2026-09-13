package com.artemis.helper;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * The session token the host pushed for this process lifetime.
 *
 * The loopback server is reachable by every app on the device, so requests are
 * only served when they carry this token. It lives in memory only: a killed or
 * re-bound service starts without one and answers 401 until the host pushes it
 * again (which the host does on every attach and on every 401).
 */
public final class TokenStore {

    private static volatile String token;

    private TokenStore() {}

    public static void set(String value) {
        token = (value == null || value.isEmpty()) ? null : value;
    }

    public static boolean isSet() {
        return token != null;
    }

    /** Constant-time comparison so a local app cannot guess the token byte by byte. */
    public static boolean matches(String candidate) {
        String current = token;
        if (current == null || candidate == null) {
            return false;
        }
        return MessageDigest.isEqual(
                current.getBytes(StandardCharsets.UTF_8),
                candidate.getBytes(StandardCharsets.UTF_8));
    }
}
