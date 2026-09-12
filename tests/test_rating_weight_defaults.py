from pathlib import Path

import config


def test_backend_rating_defaults_use_the_bingecat_provider_contract():
    assert config.MOVIE_WEIGHTS == {
        "letterboxd": 0.25,
        "trakt": 0.25,
        "tomatoes": 0,
        "popcorn": 0,
        "imdb": 0.25,
        "metacritic": 0,
        "metacriticuser": 0,
        "tmdb": 0.25,
        "rogerebert": 0,
        "myanimelist": 0,
    }
    assert config.TV_WEIGHTS == {
        "trakt": 1 / 3,
        "tomatoes": 0,
        "popcorn": 0,
        "imdb": 1 / 3,
        "metacritic": 0,
        "metacriticuser": 0,
        "tmdb": 1 / 3,
        "myanimelist": 0,
    }


def test_configurator_rating_defaults_match_backend_contract():
    html = Path("configurator.html").read_text(encoding="utf-8")
    assert "const DEFAULT_MOVIE_W = {imdb:0.25,letterboxd:0.25,tmdb:0.25,trakt:0.25," in html
    assert "const DEFAULT_TV_W    = {imdb:0.3333333333333333,tmdb:0.3333333333333333,trakt:0.3333333333333333," in html
    assert "tomatoes:0,popcorn:0" in html
    assert 'input type="range" min="0" max="1" step="any" value="${def}"' in html
    assert "`${k}:${String(v)}`" in html
