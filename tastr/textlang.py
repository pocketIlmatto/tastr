"""Which language(s) a song's lyrics are in, read from the words, for LRCLIB's lyrics (LRCLIB doesn't say).

Line by line: the writing system decides first (kana is Japanese, Hangul Korean, Thai, Greek, Hebrew, Devanagari
and so on; Chinese characters alone are Chinese, or Japanese in a song that has kana), then, for Latin and Cyrillic
letters, common words and telltale letters (ñ, ß, ł, і...). Each Latin (or Cyrillic) line is taken to be in the
song's main language for that alphabet unless its own words clearly say another one, so "oh baby" in a Spanish
song stays Spanish and a whole English chorus in it is English.

No library: the web server is a Cloudflare Worker with 128 MB of memory, and the usual detectors (langid, lingua,
fastText) load 75 MB to 1 GB of model. Short lines and languages missing from the word lists below are the weak
spots, so `languages` returns nothing rather than a guess when too few lines can be read.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

MIN_SHARE = 0.10  # a language under this share of the lines is left out (a borrowed line, a misread one)
MIN_KNOWN = 0.5  # at least this share of the wordy lines must be readable, or nothing is said
MIN_LINES = 2  # and at least this many lines
MIN_COVER = 0.08  # the main Latin/Cyrillic language must account for this much of those lines' words...
MIN_VOTES = 0.4  # ...and lean this share of those lines its way
SWITCH = 1.5  # a line leaves the main language when another scores at least this much...
MARGIN = 2.0  # ...and at least this many times the main language's score (plus a little)

# Common words of each language (lowercase). A word in several lists counts for each of them, split evenly.
_WORDS: dict[str, str] = {
    "en": "the and you i i'm me my to it in of is that your you're we be on for all love so don't can just know like "
          "what this with but are now baby get got go never when want wanna gonna feel time see way one let it's can't "
          "how need night heart there they she he her him up out down no was were have will would could if i'll ain't "
          "tonight been more back say said make only every still from at as do our us why where who ever without "
          "around through again gotta cause 'cause take give come came away stay tell think believe eyes life world "
          "right nothing something yeah girl boy i've i'd we're they're won't didn't doesn't isn't let's",
    "es": "que de la el en y a los las un una por con para mi tu te me se lo le no es yo si sí más pero como cuando "
          "todo nada quiero amor corazón vida noche estoy eres soy está estás tú contigo conmigo nunca siempre porque "
          "donde dónde qué cómo bien ya hay voy vamos ella él nos sin otra otro solo sólo tengo tienes puedo hasta del "
          "al mí ti muy baila bailar sabes dime ahora hoy así mucho también este esta eso esa aquí cielo fuego dios "
          "mujer bebé mami papi tiempo mundo ojos boca quieres sé ser hacer verte besos beso pa' mío mía tus mis",
    "pt": "que de o a os as um uma e é eu você tu ele ela nós não sim com para pra por meu minha teu tua seu sua me "
          "te se mais mas como quando tudo nada quero amor coração vida noite estou está sou são isso essa esse aqui "
          "agora sempre nunca porque onde também muito bem vai vou vem vamos ser ter tenho fazer sem só ainda já "
          "então dia saudade beijo gente mundo tempo céu do da dos das no na nos nas num numa pelo pela lá coisa "
          "contigo comigo teus meus",
    "fr": "je tu il elle nous vous ils le la les un une des de du et est en que qui pas ne ce c'est j'ai suis mon ma "
          "mes ton ta tes son sa ses moi toi lui dans pour avec sur mais plus tout tous rien comme quand oui non si "
          "on au aux cette cet être avoir fait faire veux peux sais dis vais va amour coeur cœur vie nuit jour encore "
          "toujours jamais j'aime t'aime aime quoi où là ça déjà aussi très bien mal ici monde temps yeux ciel demain "
          "n'est d'un l'amour",
    "it": "che di il la lo gli le un una uno e è non per con mi ti si ci io tu lui lei noi voi sono sei ho hai ha "
          "mio mia tuo tua suo sua ma più come quando tutto niente voglio amore cuore vita notte sempre mai perché "
          "dove anche molto bene va vai andiamo essere fare senza solo ancora già allora giorno occhi mondo tempo "
          "cielo del della dei delle nel nella al alla questo questa quello cosa così qui adesso ora ciao bella amo "
          "te c'è",
    "ca": "i el la els les un una és amb per que de del no em et es jo tu ell ella nosaltres meu meva teu teva però "
          "com quan tot res sempre mai amor cor vida nit dia sóc ets vull puc sé perquè aquí ara avui temps món ulls "
          "bé molt també encara això aquest aquesta",
    "de": "ich du er sie es wir ihr der die das den dem ein eine einen und ist nicht mit auf für von zu im in mich "
          "dich mir dir mein meine dein deine aber wie wenn was wo so noch nur auch schon alles nichts immer nie "
          "liebe herz leben nacht tag bin bist sind war hab habe hast will kann weiß weil doch ja nein oder sich uns "
          "euch hier jetzt heute gehen geht komm kommt zeit welt augen himmel gut mal dass ganz wieder",
    "nl": "ik je jij hij zij wij we het de een en is niet met op voor van te in mij me jou mijn jouw maar hoe als "
          "wat waar nog ook al alles niets altijd nooit liefde hart leven nacht dag ben bent zijn was heb hebt wil "
          "kan weet omdat toch ja nee of ons hier nu vandaag gaan gaat kom tijd wereld ogen goed er dat die dit deze "
          "naar om bij zo wel meer laat zal zou",
    "sv": "jag du han hon vi ni de det den en ett och är inte med på för av till i mig dig min mitt mina din ditt "
          "men hur när vad var här nu alla allt inget alltid aldrig kärlek hjärta liv natt dag har vill kan vet att "
          "som om så ja nej eller oss er mycket bara också igen tid världen ögon bra",
    "da": "jeg ikke mig dig og er det en et til med på af for hvad hvor når nu kun også aldrig altid kærlighed "
          "hjerte mit min din dit vil kan ved som om så ja nej eller os meget bare igen tid verden øjne godt",
    "no": "jeg ikke meg deg og er det en et til med på av for hva hvor når nå bare også aldri alltid kjærlighet "
          "hjerte min mitt din ditt vil kan vet som om så ja nei eller oss mye igjen tid verden øyne godt",
    "fi": "minä mä sinä sä hän me te he se ja on ei en et oli olen olet ole kun niin mutta että jos kanssa minun "
          "sinun mun sun rakkaus rakastan sydän elämä yö päivä aina koskaan nyt tänään tää tämä mitä missä miksi "
          "kuin vain myös vielä taas aika maailma silmät hyvä kaikki mitään",
    "pl": "ja ty on ona my wy oni nie jest się w z na do i że to co jak mnie cię ciebie mi ci mój moja twój twoja "
          "ale kiedy gdzie tylko już jeszcze zawsze nigdy miłość serce życie noc dzień jestem jesteś chcę mogę wiem "
          "bo tak czy nas tu teraz dziś czas świat oczy dobrze wszystko nic dla po przez bez od tym ten ta",
    "cs": "já ty on ona my vy oni ne je se v s na do a že to co jak mě tě tebe mi ti můj moje tvůj tvoje ale když "
          "kde jen už ještě vždy nikdy láska srdce život noc den jsem jsi chci můžu vím protože tak nás tady teď "
          "dnes čas svět oči dobře všechno nic pro po bez od",
    "ro": "eu tu el ea noi voi ei și în nu este e cu pe pentru de la un o mă te mi îți meu mea tău ta dar cum când "
          "unde doar deja încă mereu niciodată iubire dragoste inima inimă viața viață noapte zi sunt ești vreau pot "
          "știu că da aici acum azi timp lume ochii bine tot nimic fără",
    "hu": "én te ő mi ti ők a az egy és nem van is hogy de meg el már még csak mindig soha szerelem szív szívem "
          "élet éjjel éjszaka nap vagyok vagy akarok tudom mert igen itt most ma idő világ szemed jó minden semmi "
          "nekem neked veled velem engem téged",
    "tr": "ben sen o biz siz onlar bir ve bu şu da de ne mi değil var yok gibi için ile beni seni bana sana benim "
          "senin aşk aşkım kalp kalbim hayat gece gün her hiç şimdi bugün zaman dünya gözler gözlerin çok ama ki ya "
          "nasıl neden nerede olsun oldu olur gel git istiyorum seviyorum sevgilim artık hep yine",
    "id": "aku kamu kau dia kita kami mereka yang dan di ke dari ini itu tidak tak ada dengan untuk akan sudah "
          "belum cinta hati hidup malam hari selalu pernah sekarang waktu dunia mata mau bisa tahu karena jika kalau "
          "saja juga lagi semua apa mana ku mu sayang bila pun tapi namun",
    "tl": "ako ikaw siya tayo kami sila ang ng mga sa at na ay hindi ko mo ka natin akin iyo mahal kita puso buhay "
          "gabi araw lagi ngayon panahon mundo mata gusto alam kasi kung lang din rin pa ba ano saan bakit naman nga",
    "vi": "anh em yêu và là không của có một những người tôi ta mình đã đang sẽ này đó cho với khi thì mà như "
          "lại đi về nhau trong ngày đêm tình trái tim",
    "ru": "я ты он она мы вы они и в не на что с как это но меня тебя мне тебе мой моя твой твоя все всё только "
          "когда где уже ещё всегда никогда любовь сердце жизнь ночь день был была хочу могу знаю потому так да нет "
          "здесь сейчас сегодня время мир глаза хорошо ничего без для по от к у же бы ли этот эта",
    "uk": "я ти він вона ми ви вони і й в у не на що з як це але мене тебе мені тобі мій моя твій твоя все тільки "
          "коли де вже ще завжди ніколи кохання любов серце життя ніч день був була хочу можу знаю бо так ні тут "
          "зараз сьогодні час світ очі добре нічого без для по від до",
    "bg": "аз ти той тя ние вие те и в не на че с как това но мен теб мой моя твой твоя всичко само кога къде вече "
          "още винаги никога любов сърце живот нощ ден беше искам мога зная знам защото така да тук сега днес време "
          "свят очи добре нищо без за по от към ще си се е",
    "sr": "ја ти он она ми ви они и у не на да што са како то али мене тебе мени теби мој моја твој твоја све само "
          "кад када где већ још увек никад љубав срце живот ноћ дан хоћу могу знам јер тако овде сад сада данас "
          "време свет очи добро ништа без за по од је сам си смо",
}
WORDS: dict[str, tuple[str, ...]] = {}
for _lang, _list in _WORDS.items():
    for _w in _list.split():
        WORDS[_w] = WORDS.get(_w, ()) + (_lang,)

# Letters that point to a language (each counts once per line, split evenly among its languages).
LETTERS: dict[str, tuple[str, ...]] = {
    "ñ": ("es",), "¿": ("es",), "¡": ("es",), "ã": ("pt",), "õ": ("pt",), "ç": ("fr", "pt", "ca", "tr"),
    "ß": ("de",), "ä": ("de", "sv", "fi"), "ö": ("de", "sv", "fi", "tr", "hu"), "ü": ("de", "tr", "hu"),
    "å": ("sv", "da", "no"), "ø": ("da", "no"), "æ": ("da", "no"), "ł": ("pl",), "ą": ("pl",), "ę": ("pl",),
    "ż": ("pl",), "ź": ("pl",), "ś": ("pl",), "ć": ("pl",), "ń": ("pl",), "ř": ("cs",), "ě": ("cs",), "ů": ("cs",),
    "ğ": ("tr",), "ş": ("tr",), "ı": ("tr",), "ő": ("hu",), "ű": ("hu",), "ș": ("ro",), "ț": ("ro",), "ă": ("ro", "vi"),
    "ư": ("vi",), "ơ": ("vi",), "đ": ("vi",), "ạ": ("vi",), "ả": ("vi",), "ấ": ("vi",), "ầ": ("vi",), "ậ": ("vi",),
    "ế": ("vi",), "ề": ("vi",), "ệ": ("vi",), "ộ": ("vi",), "ố": ("vi",), "ồ": ("vi",), "ợ": ("vi",), "ụ": ("vi",),
    "ủ": ("vi",), "ữ": ("vi",), "ị": ("vi",), "ỉ": ("vi",),
    "ы": ("ru",), "э": ("ru",), "ё": ("ru",), "і": ("uk",), "ї": ("uk",), "є": ("uk",), "ґ": ("uk",), "ъ": ("bg", "ru"),
    "ј": ("sr",), "љ": ("sr",), "њ": ("sr",), "ђ": ("sr",), "ћ": ("sr",), "џ": ("sr",),
}
# Elided French and Italian words: "j'ai" counts for French even when the whole word isn't listed.
ELIDED = {"j": ("fr",), "qu": ("fr",), "n": ("fr",), "m": ("fr",), "t": ("fr",), "s": ("fr",), "c": ("fr", "it"),
          "l": ("fr", "it"), "d": ("fr", "it"), "un": ("it",), "dell": ("it",), "all": ("it",), "nell": ("it",),
          "sull": ("it",)}
# Sounds rather than words: a line of only these says nothing about the language and isn't counted.
VOCABLES = set("oh ooh oooh ohh yeah yeh yea ya yah la na nah da di do doo ah ahh aah uh uhh huh hey ay ayy eh mm "
               "mmm hmm woah whoa wo woo ha hah hoo ho baby".split())

# Writing systems that name a language by themselves: (first, last code point, language). Han is resolved later.
SCRIPTS = [
    (0x3040, 0x30FF, "ja"), (0x31F0, 0x31FF, "ja"), (0xFF66, 0xFF9F, "ja"),
    (0xAC00, 0xD7AF, "ko"), (0x1100, 0x11FF, "ko"), (0x3130, 0x318F, "ko"),
    (0x4E00, 0x9FFF, "han"), (0x3400, 0x4DBF, "han"), (0xF900, 0xFAFF, "han"),
    (0x0E00, 0x0E7F, "th"), (0x0370, 0x03FF, "el"), (0x1F00, 0x1FFF, "el"), (0x0590, 0x05FF, "he"),
    (0x0600, 0x06FF, "arab"), (0x0750, 0x077F, "arab"), (0x0900, 0x097F, "hi"), (0x0980, 0x09FF, "bn"),
    (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"), (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"),
    (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"), (0x0D80, 0x0DFF, "si"), (0x0E80, 0x0EFF, "lo"),
    (0x1000, 0x109F, "my"), (0x1780, 0x17FF, "km"), (0x10A0, 0x10FF, "ka"), (0x0530, 0x058F, "hy"),
    (0x1200, 0x137F, "am"),
]
PERSIAN, URDU = set("پچژگ"), set("ٹڈڑںے")

_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*'?", re.UNICODE)


def _script(ch: str) -> str | None:
    o = ord(ch)
    for lo, hi, lang in SCRIPTS:
        if lo <= o <= hi:
            return lang
    if 0x0400 <= o <= 0x04FF:
        return "cyrl"
    if ch.isalpha():
        return "latn" if unicodedata.name(ch, "").startswith("LATIN") else "unknown"
    return None


def _scripts(line: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in line:
        s = _script(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


def _scores(line: str) -> tuple[dict[str, float], int]:
    """Each language's score for a Latin or Cyrillic line from its words and letters, and its word count (sounds
    like "oh" and "yeah" left out)."""
    scores: dict[str, float] = {}

    def add(langs: tuple[str, ...], weight: float = 1.0) -> None:
        for lang in langs:
            scores[lang] = scores.get(lang, 0.0) + weight / len(langs)

    words = 0
    for tok in _TOKEN.findall(line.lower().replace("’", "'")):
        if tok in VOCABLES:
            continue
        words += 1
        if tok in WORDS:
            add(WORDS[tok])
        elif "'" in tok and tok.split("'")[0] in ELIDED:
            add(ELIDED[tok.split("'")[0]])
    for ch in set(line.lower()):
        if ch in LETTERS:
            add(LETTERS[ch], 1.5)
    return scores, words


def line_language(line: str) -> tuple[str | None, dict[str, float], int]:
    """(language, or "han" or "arab" for the song to settle, by its writing system, else None; word scores; word count) for one line."""
    counts = _scripts(line)
    if not counts:
        return None, {}, 0
    if counts.get("ja"):
        return "ja", {}, 1
    if counts.get("ko", 0) >= 2:
        return "ko", {}, 1
    other = {k: n for k, n in counts.items() if k not in ("latn", "cyrl", "ja", "ko", "unknown")}
    alpha = counts.get("latn", 0) + counts.get("cyrl", 0)
    if not other and counts.get("unknown", 0) > alpha:
        # A writing system not listed above: words this can't read, which count against `languages` saying anything
        return None, {}, max(1, len(_TOKEN.findall(line)))
    if other:
        s, n = max(other.items(), key=lambda x: x[1])
        if n * (3 if s == "han" else 1) >= alpha:  # one Chinese character is about a word
            return s, {}, 1
    scores, words = _scores(line)
    return None, scores, words


def languages(lines: Iterable[str] | str | None) -> list[dict]:
    """[{code, share}] of the lyrics' lines, the most lines first (ISO 639-1 codes, share of the lines that say a
    language), or [] when too little can be read. `lines` is the lyrics' text or its lines."""
    if not lines:
        return []
    if isinstance(lines, str):
        lines = lines.splitlines()
    read = [(line, *line_language(line)) for line in lines if line and line.strip()]
    # The main language of each alphabet: the one most lines lean to (a few English lines full of "you" and "the"
    # mustn't outvote many Spanish ones with fewer listed words), if its words cover enough of the song's.
    main: dict[str, str | None] = {}
    for alphabet in ("latn", "cyrl"):
        total: dict[str, float] = {}
        votes: dict[str, int] = {}
        words = lines = 0
        for line, lang, scores, n in read:
            if lang is None and n and _alphabet(line) == alphabet:
                words += n
                lines += 1
                for k, v in scores.items():
                    total[k] = total.get(k, 0.0) + v
                if scores:
                    top = max(scores.items(), key=lambda x: x[1])[0]
                    votes[top] = votes.get(top, 0) + 1
        best = max(votes, key=lambda k: (votes[k], total[k]), default=None)
        sure = best and total[best] >= MIN_COVER * words and votes[best] >= MIN_VOTES * lines
        main[alphabet] = best if sure else None
    labels: list[str | None] = []
    wordy = 0
    for line, lang, scores, n in read:
        if lang is None and not n:
            continue  # only sounds
        wordy += 1
        if lang is None:
            m = main.get(_alphabet(line))
            best = max(scores.items(), key=lambda x: x[1], default=(None, 0.0))
            if best[0] and best[0] != m and best[1] >= SWITCH and best[1] >= MARGIN * scores.get(m, 0.0) + 0.5:
                lang = best[0]
            else:
                lang = m
        labels.append(lang)
    if "arab" in labels:  # Persian and Urdu add letters to Arabic's, and Urdu uses Persian's too; the song decides
        text = "".join(line for line, lang, _, _ in read if lang == "arab")
        arab = "ur" if URDU & set(text) else "fa" if PERSIAN & set(text) else "ar"
        labels = [arab if x == "arab" else x for x in labels]
    if "han" in labels:
        han = "ja" if "ja" in labels else "zh"
        labels = [han if x == "han" else x for x in labels]
    known = [x for x in labels if x]
    if len(known) < MIN_LINES or len(known) < MIN_KNOWN * wordy:
        return []
    counts: dict[str, int] = {}
    for x in known:
        counts[x] = counts.get(x, 0) + 1
    kept = {k: n for k, n in counts.items() if n / len(known) >= MIN_SHARE}
    total = sum(kept.values())
    return [{"code": k, "share": round(n / total, 3)} for k, n in sorted(kept.items(), key=lambda x: (-x[1], x[0]))]


def _alphabet(line: str) -> str:
    c = _scripts(line)
    if c.get("unknown", 0) > c.get("latn", 0) + c.get("cyrl", 0):
        return "unknown"  # no main language: its lines stay unread
    return "cyrl" if c.get("cyrl", 0) > c.get("latn", 0) else "latn"
