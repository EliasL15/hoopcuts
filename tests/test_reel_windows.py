import pytest

from basketball_highlights.social_reel import merge_reel_windows


def test_union_preserves_all_selected_footage_without_repeating_overlap():
    windows = [{"start": 20, "end": 30}, {"start": 4, "end": 14},
               {"start": 0, "end": 10}, {"start": 7, "end": 9},
               {"start": 14, "end": 16}, {"start": 0, "end": 10}]
    original = [dict(window) for window in windows]
    assert merge_reel_windows(windows) == [{"start": 0, "end": 16}, {"start": 20, "end": 30}]
    assert windows == original
    assert merge_reel_windows(merge_reel_windows(windows)) == merge_reel_windows(windows)


def test_separate_baskets_keep_every_frame_and_real_gaps():
    # First two selected events are 0.75 seconds apart, but neither is deleted.
    windows = [{"start": 5849.5, "end": 5859.5}, {"start": 5850.25, "end": 5860.25},
               {"start": 5861, "end": 5871}]
    assert merge_reel_windows(windows) == [
        {"start": 5849.5, "end": 5860.25}, {"start": 5861, "end": 5871}]


@pytest.mark.parametrize("start,end", [(-1, 5), (3, 2), (2, 2.01),
                                       (float("nan"), 3), (1, float("inf"))])
def test_invalid_contained_interval_is_not_hidden(start, end):
    with pytest.raises(ValueError, match="invalid timing"):
        merge_reel_windows([{"start": 0, "end": 10}, {"start": start, "end": end}])


def test_duration_clamps_end_but_rejects_outside_selection():
    assert merge_reel_windows([{"start": 2, "end": 6}], 5) == [{"start": 2, "end": 5}]
    with pytest.raises(ValueError, match="invalid timing"):
        merge_reel_windows([{"start": 6, "end": 8}], 5)
