package com.artemis.helper;

/**
 * High-performance XML serialization and character sanitization utilities.
 * Ensures the output strictly conforms to the W3C XML 1.0 specification,
 * preventing XML parsing errors (such as ET.ParseError in Python) caused by
 * unprintable control characters, NUL bytes, or unescaped entities.
 */
public final class XmlUtils {

    private XmlUtils() {}

    /**
     * Appends an escaped attribute to the StringBuilder: {@code name="value"}.
     * If the value is null or empty, appends {@code name=""}.
     */
    public static void appendAttribute(StringBuilder sb, String name, CharSequence value) {
        sb.append(' ').append(name).append("=\"");
        if (value != null && value.length() > 0) {
            escapeXmlAttr(sb, value);
        }
        sb.append('"');
    }

    /**
     * Appends a boolean attribute: {@code name="true"} or {@code name="false"}.
     */
    public static void appendBooleanAttribute(StringBuilder sb, String name, boolean value) {
        sb.append(' ').append(name).append(value ? "=\"true\"" : "=\"false\"");
    }

    /**
     * Appends an integer attribute: {@code name="123"}.
     */
    public static void appendIntAttribute(StringBuilder sb, String name, int value) {
        sb.append(' ').append(name).append("=\"").append(value).append('"');
    }

    /**
     * Escapes and sanitizes an attribute value according to XML 1.0 rules.
     * Drops illegal XML characters (e.g. ASCII control characters 0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F)
     * and escapes standard entities (&amp;, &lt;, &gt;, &quot;, &apos;).
     */
    public static void escapeXmlAttr(StringBuilder sb, CharSequence text) {
        final int len = text.length();
        for (int i = 0; i < len; i++) {
            char c = text.charAt(i);
            switch (c) {
                case '&':
                    sb.append("&amp;");
                    break;
                case '<':
                    sb.append("&lt;");
                    break;
                case '>':
                    sb.append("&gt;");
                    break;
                case '"':
                    sb.append("&quot;");
                    break;
                case '\'':
                    sb.append("&apos;");
                    break;
                case '\t':
                    sb.append("&#9;");
                    break;
                case '\n':
                    sb.append("&#10;");
                    break;
                case '\r':
                    sb.append("&#13;");
                    break;
                default:
                    // XML 1.0 legal character range check:
                    // #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
                    if (c >= 0x20 && c <= 0xD7FF) {
                        sb.append(c);
                    } else if (c >= 0xE000 && c <= 0xFFFD) {
                        sb.append(c);
                    } else if (Character.isHighSurrogate(c)) {
                        if (i + 1 < len && Character.isLowSurrogate(text.charAt(i + 1))) {
                            sb.append(c).append(text.charAt(i + 1));
                            i++;
                        }
                    }
                    // Characters outside these ranges (e.g. \u0000 through \u0008, \u000B, \u000C, \u000E-\u001F,
                    // unpaired surrogates, \uFFFE, \uFFFF) are invalid XML 1.0 and silently omitted to guarantee well-formedness.
                    break;
            }
        }
    }

    /**
     * Sanitizes a string for safe XML attribute inclusion, returning the escaped string.
     */
    public static String escapeXmlAttr(CharSequence text) {
        if (text == null || text.length() == 0) return "";
        StringBuilder sb = new StringBuilder(text.length() + 16);
        escapeXmlAttr(sb, text);
        return sb.toString();
    }
}
