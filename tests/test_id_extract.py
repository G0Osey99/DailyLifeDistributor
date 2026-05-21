from core.refresh.id_extract import parse_url


def test_youtube_video_url():
    assert parse_url("youtube_video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

def test_youtube_short_url_form():
    assert parse_url("youtube_shorts", "https://youtube.com/shorts/AbCdEf12345") == "AbCdEf12345"

def test_youtube_short_youtu_be():
    assert parse_url("youtube_video", "https://youtu.be/abc123XYZ_-") == "abc123XYZ_-"

def test_simplecast_uuid():
    url = "https://dashboard.simplecast.com/accounts/aaa/shows/bbb/episodes/2f3f5d1c-aa24-4be4-b3c9-d12c9d88f3ad/"
    assert parse_url("simplecast", url) == "2f3f5d1c-aa24-4be4-b3c9-d12c9d88f3ad"

def test_rock_item_id():
    assert parse_url("rock", "https://rock.lcbcchurch.com/ContentChannelItem/17962") == "17962"

def test_unknown_platform_returns_none():
    assert parse_url("twitter", "https://twitter.com/foo") is None

def test_malformed_url_returns_none():
    assert parse_url("youtube_video", "not a url") is None
    assert parse_url("simplecast", "") is None
