import pytest

from chat_listener import channel_from_url, detect_platform


@pytest.mark.parametrize(
    "url,platform,channel",
    [
        ("https://www.twitch.tv/gaules", "twitch", "gaules"),
        ("twitch.tv/Gaules/", "twitch", "Gaules"),
        ("https://m.twitch.tv/some_chan?x=1", "twitch", "some_chan"),
        ("https://kick.com/xqc", "kick", "xqc"),
        ("https://www.kick.com/xqc", "kick", "xqc"),
        ("https://www.youtube.com/@Canal/live", "youtube", "Canal"),
        ("https://youtube.com/@Canal", "youtube", "Canal"),
        ("https://www.youtube.com/channel/UC123abc", "youtube", "UC123abc"),
        ("https://www.youtube.com/watch?v=abcDEF12345", "youtube", "abcDEF12345"),
        ("https://m.youtube.com/watch?v=abcDEF12345&t=3", "youtube", "abcDEF12345"),
    ],
)
def test_valid_urls(url, platform, channel):
    assert detect_platform(url) == platform
    assert channel_from_url(url) == channel


@pytest.mark.parametrize(
    "url",
    ["https://example.com/foo", "", "https://vimeo.com/123", "not a url"],
)
def test_unsupported_platform(url):
    with pytest.raises(ValueError):
        detect_platform(url)
    with pytest.raises(ValueError):
        channel_from_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://twitch.tv/", "https://kick.com", "https://www.youtube.com/", "https://youtube.com/watch"],
)
def test_no_channel(url):
    with pytest.raises(ValueError):
        channel_from_url(url)
