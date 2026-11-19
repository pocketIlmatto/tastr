"""Reading a song's language(s) from its words (tastr/textlang.py), for LRCLIB's lyrics.

Every line here is made up: real lyrics belong to their publishers and don't go in this repository.
"""

from tastr import lyrics, textlang


def codes(text):
    return [x["code"] for x in textlang.languages(text)]


def test_one_language_songs():
    assert codes("I keep on running through the night\nOh oh oh\nYou know I want you by my side\n"
                 "Don't let me go tonight") == ["en"]
    assert codes("Je ne sais pas pourquoi\nTu es partie sans moi\nEt je pense encore à toi") == ["fr"]
    assert codes("Eu não sei mais o que fazer\nVocê é tudo pra mim\nFica comigo essa noite") == ["pt"]
    assert codes("Non ho mai visto il cielo così\nSei tutto quello che voglio\nAmore mio resta qui con me") == ["it"]
    assert codes("Ich bin immer noch hier\nDu bist nicht mehr bei mir\nWir waren so jung") == ["de"]
    assert codes("Я не знаю что сказать\nТы всегда в моём сердце\nЭта ночь только для нас") == ["ru"]
    assert codes("我在路上看见你\n月亮照在窗前\n你说过的话") == ["zh"]


def test_short_lines_follow_the_songs_main_language():
    # "Mami" or "Dale" alone say nothing; they stay with the song's Spanish
    assert codes("Dale\nBaila conmigo\nMami\nElla quiere más\nToda la noche\nPa' arriba") == ["es"]


def test_mixed_songs_get_each_language_by_lines():
    got = textlang.languages("Te quiero más que nada\nCuando la noche cae sobre mí\nBailamos hasta el amanecer\n"
                             "Oh oh\nI just wanna hold you tonight\nYou're the only one I need\n"
                             "Te quiero más que nada\nMi corazón es tuyo")
    assert got == [{"code": "es", "share": 0.714}, {"code": "en", "share": 0.286}]
    # Kana is Japanese, and Chinese characters alone in a song with kana are Japanese too
    assert codes("君の声が聞こえる\n夜空に星\nBaby I need you tonight\n走り出す明日へ\nI will always be with you\n"
                 "雨の中で待ってる") == ["ja", "en"]
    assert codes("너를 사랑해\nOh baby you are my everything\n우리 함께 걸어가\n내 마음을 알아줘") == ["ko", "en"]


def test_a_borrowed_line_under_a_tenth_is_left_out():
    song = "\n".join(["You know I want you by my side", "Don't let me go tonight"] * 6 + ["Je t'aime"])
    assert codes(song) == ["en"]


def test_urdu_lines_with_letters_persian_shares_stay_urdu():
    assert codes("میں تم سے پیار کرتا ہوں\nتم میرے دل میں رہتے ہو") == ["ur"]
    assert codes("من تو را دوست دارم\nچشمهای تو را می‌بینم") == ["fa"]


def test_lines_in_an_unlisted_writing_system_count_as_unread():
    song = "I keep on running through the night\nYou know I want you by my side\n" + "ང་ཁྱོད་ལ་དགའ་པོ་ཡོད\n" * 10
    assert textlang.languages(song) == []


def test_nothing_when_the_words_dont_say():
    assert textlang.languages("la la la\nna na na\noh oh yeah") == []
    assert textlang.languages("Nakupenda sana mpenzi wangu\nMoyo wangu unakupenda\nUsiniache peke yangu") == []
    assert textlang.languages("Hello") == []
    assert textlang.languages("") == [] and textlang.languages(None) == []


def test_lrclib_answers_carry_their_languages_and_old_ones_get_them():
    hit = {"id": 1, "trackName": "T", "artistName": "A", "duration": 100.0, "instrumental": False,
           "plainLyrics": "Je ne sais pas pourquoi\nTu es partie sans moi", "syncedLyrics": None}
    got = lyrics.result(hit, lyrics.LRCLIB_PROVIDER)
    assert got["languages"] == [{"code": "fr", "share": 1.0}]
    old = {k: v for k, v in got.items() if k != "languages"} | {"fetched_at": "x"}
    assert lyrics.describe(old)["languages"] == [{"code": "fr", "share": 1.0}]
    assert lyrics.result(hit | {"instrumental": True, "plainLyrics": None}, lyrics.LRCLIB_PROVIDER)["languages"] == []
    # What Whisper heard keeps Whisper's own languages
    heard = {"provider": "whisper", "status": "found", "plain": "Je ne sais pas pourquoi\nTu es partie sans moi",
             "synced": [], "languages": [{"code": "en", "share": 1.0}]}
    assert lyrics.describe(heard)["languages"] == [{"code": "en", "share": 1.0}]
