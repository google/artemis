package com.artemis.helper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * In-memory snapshot of an Accessibility UI element node.
 * Decoupled from live Android AccessibilityNodeInfo objects to prevent
 * Binder proxy memory leaks, avoid stale node crashes, and ensure
 * instant thread-safe serialization to both XML and JSON.
 *
 * {@code bounds} are the node's <em>visible</em> bounds: the raw
 * {@code getBoundsInScreen} rectangle intersected with the display, the
 * node's window and every scrollable ancestor, exactly like UIAutomator's
 * {@code getVisibleBoundsInScreen}. They are therefore never negative and
 * never extend past the screen.
 */
public final class A11yNode {

    public int index = 0;
    public String text = "";
    public String resourceId = "";
    public String className = "";
    public String packageName = "";
    public String contentDesc = "";

    public boolean checkable = false;
    public boolean checked = false;
    public boolean clickable = false;
    public boolean enabled = true;
    public boolean focusable = false;
    public boolean focused = false;
    public boolean scrollable = false;
    public boolean longClickable = false;
    public boolean password = false;
    public boolean selected = false;
    public boolean visibleToUser = true;

    // Window metadata for multi-window awareness
    public int windowId = -1;
    public String windowType = "";
    public int windowLayer = 0;
    public boolean windowActive = false;
    public boolean windowFocused = false;

    // Advanced semantics for Agent perception & verification
    public boolean editable = false;
    public boolean isHeading = false;
    public boolean screenReaderFocusable = false;
    public String stateDescription = "";
    public String errorText = "";
    public String paneTitle = "";
    public String tooltip = "";

    public int left = 0;
    public int top = 0;
    public int right = 0;
    public int bottom = 0;

    public int drawingOrder = 0;
    public String hint = "";

    public final List<A11yNode> children = new ArrayList<>(4);

    public A11yNode() {}

    public int getWidth() {
        return right - left;
    }

    public int getHeight() {
        return bottom - top;
    }

    public String getBoundsString() {
        return "[" + left + "," + top + "][" + right + "," + bottom + "]";
    }

    /**
     * Serializes this node and all of its descendants into standard UIAutomator XML.
     */
    public void writeXml(StringBuilder sb) {
        sb.append("<node");
        XmlUtils.appendIntAttribute(sb, "index", index);
        XmlUtils.appendAttribute(sb, "text", text);
        XmlUtils.appendAttribute(sb, "resource-id", resourceId);
        XmlUtils.appendAttribute(sb, "class", className);
        XmlUtils.appendAttribute(sb, "package", packageName);
        XmlUtils.appendAttribute(sb, "content-desc", contentDesc);
        XmlUtils.appendBooleanAttribute(sb, "checkable", checkable);
        XmlUtils.appendBooleanAttribute(sb, "checked", checked);
        XmlUtils.appendBooleanAttribute(sb, "clickable", clickable);
        XmlUtils.appendBooleanAttribute(sb, "enabled", enabled);
        XmlUtils.appendBooleanAttribute(sb, "focusable", focusable);
        XmlUtils.appendBooleanAttribute(sb, "focused", focused);
        XmlUtils.appendBooleanAttribute(sb, "scrollable", scrollable);
        XmlUtils.appendBooleanAttribute(sb, "long-clickable", longClickable);
        XmlUtils.appendBooleanAttribute(sb, "password", password);
        XmlUtils.appendBooleanAttribute(sb, "selected", selected);
        XmlUtils.appendBooleanAttribute(sb, "visible-to-user", visibleToUser);
        XmlUtils.appendAttribute(sb, "bounds", getBoundsString());
        XmlUtils.appendIntAttribute(sb, "drawing-order", drawingOrder);
        if (!hint.isEmpty()) {
            XmlUtils.appendAttribute(sb, "hint", hint);
        }

        // Window metadata (emitted on window root nodes)
        if (windowId >= 0) {
            XmlUtils.appendIntAttribute(sb, "window-id", windowId);
            if (!windowType.isEmpty()) {
                XmlUtils.appendAttribute(sb, "window-type", windowType);
            }
            XmlUtils.appendIntAttribute(sb, "window-layer", windowLayer);
            if (windowActive) {
                XmlUtils.appendBooleanAttribute(sb, "window-active", true);
            }
            if (windowFocused) {
                XmlUtils.appendBooleanAttribute(sb, "window-focused", true);
            }
        }

        // Extended semantic attributes
        if (editable) {
            XmlUtils.appendBooleanAttribute(sb, "editable", true);
        }
        if (isHeading) {
            XmlUtils.appendBooleanAttribute(sb, "heading", true);
        }
        if (screenReaderFocusable) {
            XmlUtils.appendBooleanAttribute(sb, "screen-reader-focusable", true);
        }
        if (!stateDescription.isEmpty()) {
            XmlUtils.appendAttribute(sb, "state-description", stateDescription);
        }
        if (!errorText.isEmpty()) {
            XmlUtils.appendAttribute(sb, "error", errorText);
        }
        if (!paneTitle.isEmpty()) {
            XmlUtils.appendAttribute(sb, "pane-title", paneTitle);
        }
        if (!tooltip.isEmpty()) {
            XmlUtils.appendAttribute(sb, "tooltip", tooltip);
        }

        if (children.isEmpty()) {
            sb.append(" />");
        } else {
            sb.append('>');
            for (int i = 0; i < children.size(); i++) {
                children.get(i).writeXml(sb);
            }
            sb.append("</node>");
        }
    }

    /**
     * Serializes this node into a hierarchical JSON tree object.
     */
    public JSONObject toTreeJson() {
        JSONObject obj = toBaseJson();
        try {
            if (!children.isEmpty()) {
                JSONArray childrenArr = new JSONArray();
                for (int i = 0; i < children.size(); i++) {
                    childrenArr.put(children.get(i).toTreeJson());
                }
                obj.put("children", childrenArr);
            }
        } catch (Throwable ignored) {}
        return obj;
    }

    /**
     * Serializes this node into an element JSON object for the flat list.
     * Does NOT include child references.
     */
    public JSONObject toFlatElementJson() {
        return toBaseJson();
    }

    private JSONObject toBaseJson() {
        JSONObject obj = new JSONObject();
        try {
            obj.put("class", className);
            obj.put("package", packageName);
            obj.put("resource-id", resourceId);
            obj.put("text", text);
            obj.put("content-desc", contentDesc);
            obj.put("bounds", getBoundsString());

            JSONObject bounds = new JSONObject();
            bounds.put("left", left);
            bounds.put("top", top);
            bounds.put("right", right);
            bounds.put("bottom", bottom);
            obj.put("parsed_bounds", bounds);

            obj.put("clickable", clickable);
            obj.put("scrollable", scrollable);
            obj.put("checkable", checkable);
            obj.put("checked", checked);
            obj.put("enabled", enabled);
            obj.put("focusable", focusable);
            obj.put("focused", focused);
            obj.put("selected", selected);
            obj.put("password", password);
            obj.put("long-clickable", longClickable);
            obj.put("visible-to-user", visibleToUser);
            obj.put("drawing-order", drawingOrder);
            if (!hint.isEmpty()) {
                obj.put("hint", hint);
            }

            // Window metadata
            if (windowId >= 0) {
                obj.put("window_id", windowId);
                obj.put("window_type", windowType);
                obj.put("window_layer", windowLayer);
                if (windowActive) obj.put("window_active", true);
                if (windowFocused) obj.put("window_focused", true);
            }

            // Extended semantics
            if (editable) {
                obj.put("editable", true);
            }
            if (isHeading) {
                obj.put("is_heading", true);
            }
            if (screenReaderFocusable) {
                obj.put("screen_reader_focusable", true);
            }
            if (!stateDescription.isEmpty()) {
                obj.put("state_description", stateDescription);
            }
            if (!errorText.isEmpty()) {
                obj.put("error", errorText);
            }
            if (!paneTitle.isEmpty()) {
                obj.put("pane_title", paneTitle);
            }
            if (!tooltip.isEmpty()) {
                obj.put("tooltip", tooltip);
            }
        } catch (Throwable ignored) {}
        return obj;
    }

    /**
     * Determines whether this node contains useful information or interactivity
     * and should be included in the flat elements array for agent perception.
     */
    public boolean isInformativeOrInteractive() {
        boolean hasContent = !text.isEmpty() || !contentDesc.isEmpty() || !resourceId.isEmpty()
                || !stateDescription.isEmpty() || !errorText.isEmpty() || !paneTitle.isEmpty();
        boolean isInteractive = clickable || scrollable || checkable || focusable || longClickable || editable;
        return hasContent || isInteractive || isHeading;
    }

    /**
     * Recursively collects all informative or interactive nodes into a flat list.
     */
    public void collectFlatElements(List<JSONObject> flatList) {
        if (isInformativeOrInteractive()) {
            flatList.add(toFlatElementJson());
        }
        for (int i = 0; i < children.size(); i++) {
            children.get(i).collectFlatElements(flatList);
        }
    }
}
