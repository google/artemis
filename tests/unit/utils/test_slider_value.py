from __future__ import annotations

from PIL import Image, ImageDraw

from artemis.utils.slider_value import measure_fill_percent

TRACK = (554, 338, 1138, 386)


def _slider_image(percent: int | None) -> Image.Image:
    image = Image.new("RGB", (1920, 1080), (30, 30, 34))
    draw = ImageDraw.Draw(image)
    draw.rectangle(TRACK, fill=(214, 214, 218))
    if percent is not None:
        left, top, right, bottom = TRACK
        filled_right = left + (right - left) * percent // 100
        draw.rectangle((left, top, filled_right, bottom), fill=(59, 130, 246))
    return image


def test_a_half_filled_track_reads_as_about_half() -> None:
    assert measure_fill_percent(_slider_image(44), TRACK) == 44


def test_a_full_track_reads_as_one_hundred() -> None:
    assert measure_fill_percent(_slider_image(100), TRACK) == 100


def test_an_empty_track_has_no_readable_value() -> None:
    assert measure_fill_percent(_slider_image(None), TRACK) is None


def test_a_monochrome_control_has_no_readable_value() -> None:
    image = Image.new("RGB", (1920, 1080), (200, 200, 200))

    assert measure_fill_percent(image, TRACK) is None


def test_a_glyph_inside_the_fill_does_not_shorten_the_reading() -> None:
    image = _slider_image(70)
    draw = ImageDraw.Draw(image)
    draw.rectangle((TRACK[0] + 8, TRACK[1] + 8, TRACK[0] + 34, TRACK[3] - 8), fill=(255, 255, 255))

    assert measure_fill_percent(image, TRACK) == 70


def test_a_target_share_maps_onto_the_track() -> None:
    from artemis.utils.slider_value import point_at_share

    assert point_at_share(TRACK, 70, 1920, 1080) == [501, 335]
    assert point_at_share(TRACK, 0, 1920, 1080)[0] == 289
    assert point_at_share(TRACK, 100, 1920, 1080)[0] == 593


def test_the_press_point_moves_onto_the_thumb() -> None:
    from artemis.utils.slider_value import snap_press_point

    controls = [(TRACK, 20)]

    assert snap_press_point([319, 335], controls, 1920, 1080) == [349, 335]


def test_a_press_point_outside_every_control_is_left_alone() -> None:
    from artemis.utils.slider_value import snap_press_point

    controls = [(TRACK, 20)]

    assert snap_press_point([100, 800], controls, 1920, 1080) == [100, 800]


def test_a_goal_names_its_percentage() -> None:
    from artemis.utils.slider_value import share_from_text

    assert share_from_text("滑动中控亮度调节条到约70%位置") == 70
    assert share_from_text("调整音量") is None
