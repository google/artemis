package com.artemis.helper;

import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.PathMeasure;
import android.os.Handler;
import android.os.Looper;
import android.view.accessibility.AccessibilityEvent;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.Implementation;
import org.robolectric.annotation.Implements;
import org.robolectric.annotation.LooperMode;
import org.robolectric.annotation.GraphicsMode;
import java.util.ArrayList;
import java.util.List;
import java.time.Duration;
import java.util.concurrent.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = 28, shadows = ContinuousGestureTest.InputShadow.class)
@LooperMode(LooperMode.Mode.PAUSED)
public class ContinuousGestureTest {
    private ExecutorService worker;
    private TestService service;

    public static class TestService extends AccessibilityService {
        @Override public void onAccessibilityEvent(AccessibilityEvent event) {}
        @Override public void onInterrupt() {}
    }

    @Implements(AccessibilityService.class)
    public static class InputShadow {
        static final List<GestureDescription> inputs = new ArrayList<>();
        static final List<AccessibilityService.GestureResultCallback> callbacks = new ArrayList<>();
        static int rejectIndex = -1;
        @Implementation
        protected boolean dispatchGesture(GestureDescription gesture,
                AccessibilityService.GestureResultCallback callback, Handler handler) {
            inputs.add(gesture); callbacks.add(callback);
            return inputs.size() - 1 != rejectIndex;
        }
    }

    @Before public void setup() {
        InputShadow.inputs.clear(); InputShadow.callbacks.clear(); InputShadow.rejectIndex = -1;
        GestureController.INPUT_BUSY.set(false);
        service = Robolectric.buildService(TestService.class).create().get();
        worker = Executors.newSingleThreadExecutor();
    }
    @After public void teardown() { worker.shutdownNow(); }

    private static JSONObject phase(int duration, int... points) throws Exception {
        JSONArray path = new JSONArray();
        for (int i = 0; i < points.length; i += 2)
            path.put(new JSONArray().put(points[i]).put(points[i + 1]));
        return new JSONObject().put("duration_ms", duration).put("pointers", new JSONArray().put(
                new JSONObject().put("id", 0).put("path", path)));
    }
    private static JSONArray drag() throws Exception {
        return new JSONArray().put(phase(600, 300, 400))
                .put(phase(800, 300, 400, 980, 400)).put(phase(900, 980, 400));
    }
    private Future<JSONObject> execute(String id, JSONArray phases) throws Exception {
        JSONObject args = new JSONObject().put("request_id", id).put("phases", phases);
        return worker.submit(() -> ContinuousGesture.execute(service, args));
    }
    private void awaitInputs(int count) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (InputShadow.inputs.size() < count && System.nanoTime() < deadline) {
            shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(5);
        }
        assertEquals(count, InputShadow.inputs.size());
    }
    private void complete(int index) {
        InputShadow.callbacks.get(index).onCompleted(InputShadow.inputs.get(index));
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(
                InputShadow.inputs.get(index).getStroke(0).getDuration()));
    }

    @Test public void earlyDownCallbackCannotSkipLongPressTime() throws Exception {
        Future<JSONObject> result = execute("early-down", new JSONArray().put(
                longPressDrag(0).put("hold_ms", 1500)));
        awaitInputs(1);
        InputShadow.callbacks.get(0).onCompleted(InputShadow.inputs.get(0));
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1499));
        assertEquals("Do not move while the icon is still being long-pressed", 1, InputShadow.inputs.size());
        assertTrue(GestureController.INPUT_BUSY.get());
        assertFalse(result.isDone());
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1));
        awaitInputs(2);
        complete(1);
        assertTrue(result.get(5, TimeUnit.SECONDS).getBoolean("release_confirmed"));
    }

    @Test public void cancellationWhileWaitingForLongPressReleasesWithoutMoving() throws Exception {
        Future<JSONObject> result = execute("cancel-hold-wait", new JSONArray().put(
                longPressDrag(0).put("hold_ms", 1500)));
        awaitInputs(1);
        InputShadow.callbacks.get(0).onCompleted(InputShadow.inputs.get(0));
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500));
        ContinuousGesture.cancel("cancel-hold-wait");
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1000));
        awaitInputs(2);
        assertEquals(1, InputShadow.inputs.get(1).getStroke(0).getDuration());
        assertFalse(InputShadow.inputs.get(1).getStroke(0).willContinue());
        complete(1);
        JSONObject receipt = result.get(5, TimeUnit.SECONDS);
        assertEquals("cancelled", receipt.getString("status"));
        assertTrue(receipt.getBoolean("release_confirmed"));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    @Test public void allFingersAreSimultaneousAndLiftTogether() throws Exception {
        JSONObject phase = phase(500, 100, 200, 100, 800);
        JSONArray pointers = phase.getJSONArray("pointers");
        for (int id = 1; id < 3; id++) {
            JSONObject p = new JSONObject(pointers.getJSONObject(0).toString()).put("id", id);
            p.put("path", new JSONArray().put(new JSONArray().put(100 + id * 250).put(200))
                    .put(new JSONArray().put(100 + id * 250).put(800)));
            pointers.put(p);
        }
        GestureDescription gesture = ContinuousGesture.parse(new JSONArray().put(phase), 1080, 2400).get(0).gesture;
        assertEquals(3, gesture.getStrokeCount());
        for (int i = 0; i < 3; i++) {
            assertEquals(0, gesture.getStroke(i).getStartTime());
            assertEquals(500, gesture.getStroke(i).getDuration());
            assertFalse(gesture.getStroke(i).willContinue());
        }
    }

    @Test public void dragKeepsContactUntilFinalPhase() throws Exception {
        Future<JSONObject> result = execute("drag", drag());
        for (int i = 0; i < 3; i++) {
            awaitInputs(i + 1);
            assertEquals(i < 2, InputShadow.inputs.get(i).getStroke(0).willContinue());
            assertTrue(GestureController.INPUT_BUSY.get());
            complete(i);
        }
        assertTrue(result.get(5, TimeUnit.SECONDS).getBoolean("success"));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    private static JSONArray curvedDrag() throws Exception {
        JSONArray phases = drag();
        phases.getJSONObject(1).getJSONArray("pointers").getJSONObject(0).put("control_points",
                new JSONArray("[[450,200],[800,200]]"));
        return phases;
    }

    private static JSONObject longPressDrag(int delay) throws Exception {
        return new JSONObject().put("kind", "long_press_drag").put("start", new JSONArray("[300,400]"))
                .put("end", new JSONArray("[650,550]")).put("duration_ms", 800).put("release_delay_ms", delay);
    }

    @Test public void zeroDelayLiftsOnMovementWithoutAnotherPhase() throws Exception {
        Future<JSONObject> result = execute("drag-immediate", new JSONArray().put(longPressDrag(0)));
        awaitInputs(1);
        assertTrue(InputShadow.inputs.get(0).getStroke(0).willContinue());
        complete(0);
        awaitInputs(2);
        GestureDescription.StrokeDescription movement = InputShadow.inputs.get(1).getStroke(0);
        assertEquals(800, movement.getDuration());
        assertFalse(movement.willContinue());
        complete(1);
        assertTrue(result.get(5, TimeUnit.SECONDS).getBoolean("release_confirmed"));
        assertEquals(2, InputShadow.inputs.size());
    }

    @Test public void positiveDelayHoldsAtEndpointAndOnlyThenReleases() throws Exception {
        Future<JSONObject> result = execute("drag-delayed", new JSONArray().put(longPressDrag(1200)));
        awaitInputs(1); complete(0); awaitInputs(2);
        assertTrue(InputShadow.inputs.get(1).getStroke(0).willContinue());
        assertEquals(800, InputShadow.inputs.get(1).getStroke(0).getDuration());
        complete(1); awaitInputs(3);
        GestureDescription.StrokeDescription dwell = InputShadow.inputs.get(2).getStroke(0);
        assertEquals(1200, dwell.getDuration());
        assertFalse(dwell.willContinue());
        assertFalse(result.isDone());
        assertTrue(GestureController.INPUT_BUSY.get());
        complete(2);
        assertTrue(result.get(5, TimeUnit.SECONDS).getBoolean("release_confirmed"));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    @Test @GraphicsMode(GraphicsMode.Mode.NATIVE)
    public void longPressDragPreservesChosenEndpointAndDeviceHold() throws Exception {
        for (int[] endpoint : new int[][]{{650, 550}, {400, 200}, {20, 400}, {980, 400}}) {
            for (int[] size : new int[][]{{1080, 2400}, {2400, 1080}}) {
                JSONObject intent = longPressDrag(1200).put("end", new JSONArray(endpoint))
                        .put("control_points", new JSONArray("[[400,250],[600,250]]"));
                JSONArray raw = LongPressDrag.expand(intent, 1000);
                assertEquals(1150, raw.getJSONObject(0).getInt("duration_ms"));
                List<ContinuousGesture.Phase> parsed = ContinuousGesture.parse(raw, size[0], size[1]);
                float[] end = parsed.get(1).ends.get(0);
                int nx = endpoint[0], ny = endpoint[1];
                assertArrayEquals(new float[]{nx * (size[0] - 1) / 1000f, ny * (size[1] - 1) / 1000f}, end, 0.1f);
                PathMeasure curve = new PathMeasure(parsed.get(1).paths.get(0), false);
                float[] actualEnd = new float[2];
                assertTrue(curve.getPosTan(curve.getLength(), actualEnd, null));
                assertArrayEquals(end, actualEnd, 0.1f);
                PathMeasure still = new PathMeasure(parsed.get(2).paths.get(0), false);
                assertEquals(0f, still.getLength(), 0.01f);
                assertArrayEquals(end, parsed.get(2).ends.get(0), 0.01f);
                intent.put("hold_ms", 650);
                assertEquals(650, LongPressDrag.expand(intent, 1000).getJSONObject(0).getInt("duration_ms"));
            }
        }
    }

    @Test public void invalidReleaseDelayAndMixedPlansNeverTouchDevice() throws Exception {
        for (Object value : new Object[]{-1, 5001, 1.5, true, "1000"}) {
            JSONObject invalid = longPressDrag(0).put("release_delay_ms", value);
            assertEquals("invalid_arguments", execute("bad-delay", new JSONArray().put(invalid))
                    .get(5, TimeUnit.SECONDS).getString("status"));
        }
        assertEquals("invalid_arguments", execute("mixed", new JSONArray().put(longPressDrag(0)).put(phase(100, 300, 400)))
                .get(5, TimeUnit.SECONDS).getString("status"));
        assertTrue(InputShadow.inputs.isEmpty());
    }

    @Test public void invalidLongPressDragEndpointsAndControlsNeverTouchDevice() throws Exception {
        JSONObject missingEnd = longPressDrag(0);
        missingEnd.remove("end");
        JSONObject[] invalid = {
            missingEnd,
            longPressDrag(0).put("end", new JSONArray("[1001,500]")),
            longPressDrag(0).put("end", new JSONArray("[500]")),
            longPressDrag(0).put("end", new JSONArray("[true,500]")),
            longPressDrag(0).put("control_points", new JSONArray("[[100,200]]")),
            longPressDrag(0).put("control_points", new JSONArray("[[100,200],[300,1001]]")),
            longPressDrag(0).put("direction", "left")
        };
        for (int i = 0; i < invalid.length; i++)
            assertEquals("invalid_arguments", execute("bad-drag-" + i, new JSONArray().put(invalid[i]))
                    .get(5, TimeUnit.SECONDS).getString("status"));
        assertTrue(InputShadow.inputs.isEmpty());
    }

    @Test public void longPressDragWithoutControlsUsesAStraightPath() throws Exception {
        JSONArray phases = LongPressDrag.expand(longPressDrag(0), 500);
        JSONObject movement = phases.getJSONObject(1).getJSONArray("pointers").getJSONObject(0);
        assertEquals("[[300,400],[650,550]]", movement.getJSONArray("path").toString());
        assertFalse(movement.has("control_points"));
    }

    @Test public void cancelDuringEndpointDwellStillReleasesAndUnlocks() throws Exception {
        Future<JSONObject> result = execute("drag-cancel", new JSONArray().put(longPressDrag(1000)));
        awaitInputs(1); complete(0); awaitInputs(2); complete(1); awaitInputs(3);
        ContinuousGesture.cancel("drag-cancel");
        complete(2);
        JSONObject receipt = result.get(5, TimeUnit.SECONDS);
        assertEquals("cancelled", receipt.getString("status"));
        assertTrue(receipt.getBoolean("release_confirmed"));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    @Test @GraphicsMode(GraphicsMode.Mode.NATIVE)
    public void cubicPathHasCurvatureAndScaledEndpointsWithoutLifting() throws Exception {
        List<ContinuousGesture.Phase> phases = ContinuousGesture.parse(curvedDrag(), 1001, 2001);
        PathMeasure measure = new PathMeasure(phases.get(1).paths.get(0), false);
        assertTrue(measure.getLength() > 680);
        float[] position = new float[2];
        assertTrue(measure.getPosTan(0, position, null));
        assertArrayEquals(new float[]{300, 800}, position, 0.1f);
        assertTrue(measure.getPosTan(measure.getLength() / 2, position, null));
        assertTrue("Curve must bow above the straight horizontal path", position[1] < 700);
        assertTrue(measure.getPosTan(measure.getLength(), position, null));
        assertArrayEquals(new float[]{980, 800}, position, 0.1f);
        for (int i = 0; i < phases.size(); i++)
            assertEquals(i < 2, phases.get(i).gesture.getStroke(0).willContinue());
    }

    @Test public void curvedDragCompletesHoldMoveDwellAsOneContact() throws Exception {
        Future<JSONObject> result = execute("curve", curvedDrag());
        for (int i = 0; i < 3; i++) {
            awaitInputs(i + 1);
            assertEquals(i < 2, InputShadow.inputs.get(i).getStroke(0).willContinue());
            complete(i);
        }
        JSONObject receipt = result.get(5, TimeUnit.SECONDS);
        assertTrue(receipt.getBoolean("success"));
        assertTrue(receipt.getBoolean("release_confirmed"));
    }

    @Test public void malformedCurveInLaterPhaseNeverDispatchesHold() throws Exception {
        String[] invalid = {"[]", "[[1,2]]", "[[1,2],[3,4],[5,6]]", "[[1001,2],[3,4]]",
                "[[1.5,2],[3,4]]", "[[true,2],[3,4]]", "[[1,2,3],[4,5]]"};
        for (int i = 0; i < invalid.length; i++) {
            JSONArray phases = curvedDrag();
            phases.getJSONObject(1).getJSONArray("pointers").getJSONObject(0)
                    .put("control_points", new JSONArray(invalid[i]));
            assertEquals("invalid_arguments", execute("bad-curve-" + i, phases)
                    .get(5, TimeUnit.SECONDS).getString("status"));
        }
        JSONArray phases = curvedDrag();
        phases.getJSONObject(1).getJSONArray("pointers").getJSONObject(0)
                .getJSONArray("path").put(new JSONArray("[980,400]"));
        assertEquals("invalid_arguments", execute("extra-endpoint", phases)
                .get(5, TimeUnit.SECONDS).getString("status"));
        assertTrue(InputShadow.inputs.isEmpty());
    }

    @Test public void laterInvalidPhaseCannotCausePartialExecution() throws Exception {
        JSONArray phases = drag();
        phases.getJSONObject(2).getJSONArray("pointers").getJSONObject(0)
                .put("path", new JSONArray().put(new JSONArray().put(500).put(400)));
        JSONObject result = execute("invalid", phases).get(5, TimeUnit.SECONDS);
        assertEquals("invalid_arguments", result.getString("status"));
        assertTrue(InputShadow.inputs.isEmpty());
    }

    @Test public void cancelHeldPhaseDispatchesOnlyReleaseThenUnlocks() throws Exception {
        Future<JSONObject> result = execute("cancel", drag());
        awaitInputs(1);
        ContinuousGesture.cancel("cancel");
        complete(0);
        awaitInputs(2);
        GestureDescription release = InputShadow.inputs.get(1);
        assertEquals(1, release.getStroke(0).getDuration());
        assertFalse(release.getStroke(0).willContinue());
        complete(1);
        JSONObject reply = result.get(5, TimeUnit.SECONDS);
        assertEquals("cancelled", reply.getString("status"));
        assertTrue(reply.getBoolean("release_confirmed"));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    @Test public void cancelBeforeRequestDoesNotTouchDevice() throws Exception {
        ContinuousGesture.cancel("race");
        JSONObject result = execute("race", drag()).get(5, TimeUnit.SECONDS);
        assertEquals("cancelled", result.getString("status"));
        assertTrue(InputShadow.inputs.isEmpty());
    }

    @Test public void rejectedReleaseDoesNotAllowAnotherGesture() throws Exception {
        Future<JSONObject> result = execute("reject-release", drag());
        awaitInputs(1);
        InputShadow.rejectIndex = 1;
        ContinuousGesture.cancel("reject-release");
        complete(0);
        JSONObject reply = result.get(5, TimeUnit.SECONDS);
        assertFalse(reply.getBoolean("release_confirmed"));
        assertTrue(GestureController.INPUT_BUSY.get());
        assertEquals("busy", execute("next", drag()).get(5, TimeUnit.SECONDS).getString("status"));
    }

    @Test public void ordinaryTapRetainsSuccessAndCancellationResults() throws Exception {
        Future<Boolean> tap = worker.submit(() -> GestureController.tap(service, 300, 400, 5000));
        awaitInputs(1);
        assertFalse("Ordinary actions do not own continuous-input state", GestureController.INPUT_BUSY.get());
        complete(0);
        assertTrue(tap.get(5, TimeUnit.SECONDS));

        Future<Boolean> swipe = worker.submit(() -> GestureController.swipe(service, 300, 400, 650, 550, 800, 5000));
        awaitInputs(2);
        InputShadow.callbacks.get(1).onCancelled(InputShadow.inputs.get(1));
        assertFalse(swipe.get(5, TimeUnit.SECONDS));
        assertFalse(GestureController.INPUT_BUSY.get());
    }

    @Test public void ordinaryCallbackCannotUnlockAnActiveContinuousGesture() throws Exception {
        CompletableFuture<Boolean> tap = CompletableFuture.supplyAsync(
                () -> GestureController.tap(service, 300, 400, 5000));
        awaitInputs(1);
        Future<JSONObject> gesture = execute("legacy-callback", drag());
        awaitInputs(2);
        InputShadow.callbacks.get(0).onCompleted(InputShadow.inputs.get(0));
        assertTrue(tap.get(5, TimeUnit.SECONDS));
        assertTrue(GestureController.INPUT_BUSY.get());
        assertFalse(GestureController.tap(service, 600, 500, 100));
        assertFalse(GestureController.swipe(service, 300, 400, 650, 550, 800, 100));
        assertEquals(2, InputShadow.inputs.size());

        ContinuousGesture.cancel("legacy-callback");
        complete(1);
        awaitInputs(3);
        complete(2);
        assertTrue(gesture.get(5, TimeUnit.SECONDS).getBoolean("release_confirmed"));
        assertFalse(GestureController.INPUT_BUSY.get());
        Future<Boolean> next = worker.submit(() -> GestureController.tap(service, 600, 500, 5000));
        awaitInputs(4);
        complete(3);
        assertTrue(next.get(5, TimeUnit.SECONDS));
    }

    @Test @Config(sdk = 24) public void android7RejectsContinuationButAllowsSinglePhase() throws Exception {
        assertEquals(1, ContinuousGesture.parse(new JSONArray().put(phase(200, 100, 200)), 1080, 2400).size());
        assertThrows(UnsupportedOperationException.class, () -> ContinuousGesture.parse(drag(), 1080, 2400));
    }
}
