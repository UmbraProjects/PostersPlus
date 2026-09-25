#awards.py
import os
import math
import numpy as np
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from typing import Any

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

try:
    import cairo as _cairo
    _HAS_CAIRO = True
except ImportError:
    _HAS_CAIRO = False

# Skia draws the diagonal sash at 1x with its own anti-aliasing (see
# _sash_skia).  Without it — the wheel missing, or libEGL/libGL absent so the
# module cannot load — the sash falls back to the PIL path, supersampled at 3x:
# the same sash, ~3x the cost.
try:
    import skia as _skia
    _SKIA_TYPEFACE = _skia.Typeface.MakeFromFile(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"))
    _HAS_SKIA = _SKIA_TYPEFACE is not None
except (ImportError, OSError):
    _HAS_SKIA = False


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

class _FetchFailed:
    """Singleton sentinel returned when a fetch attempt fails."""
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def __repr__(self):
        return "FETCH_FAILED"

FETCH_FAILED = _FetchFailed()


class _RateLimited:
    """
    Returned when a fetch was refused with HTTP 429 or 503.

    Carries the parsed Retry-After value (in seconds) when the upstream
    provided one, so the caller can honour it instead of the default fixed
    back-off. Always distinct from FETCH_FAILED so the standard retry path
    skips immediate re-attempts (retrying a 429 is counterproductive).

    *reset_at* is the epoch second the key's daily quota rolls over, taken
    from MDBList's X-RateLimit-Reset header. It is set only when the response
    said the key's quota is spent; that is what separates the two throttles
    MDBList applies. A quota 429 comes with no Retry-After and belongs to the
    key. A burst 429 (Retry-After, typically 10 s, no quota headers) or a 503
    is MDBList's short per-IP limit: every key on the address is refused for
    the same few seconds, so it belongs to the process, not to the key.
    """
    __slots__ = ("retry_after", "reset_at")

    def __init__(self, retry_after: float | None = None, reset_at: float | None = None):
        self.retry_after = retry_after
        self.reset_at = reset_at

    @property
    def quota_exhausted(self) -> bool:
        return self.reset_at is not None

    def __repr__(self):
        return f"RATE_LIMITED(retry_after={self.retry_after}, reset_at={self.reset_at})"


# ---------------------------------------------------------------------------
# Emmy winners — hardcoded TMDB IDs
# Drama, Comedy and Limited Series winners only.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Golden Globe Best Motion Picture – Drama — hardcoded TMDB IDs
# Sourced from: themoviedb.org/award/4-the-golden-globe-awards/category/7
# Winners: all years available. Nominees: 2006 onwards (complete data).
# Update annually after the ceremony.
# ---------------------------------------------------------------------------

GOLDEN_GLOBE_DRAMA_WINNER_TMDB_IDS: set[int] = {
    858024,  # Hamnet (2026)
    549509,  # The Brutalist (2025)
    872585,  # Oppenheimer (2024)
    804095,  # The Fabelmans (2023)
    600583,  # The Power of the Dog (2022)
    581734,  # Nomadland (2021)
    530915,  # 1917 (2020)
    424694,  # Bohemian Rhapsody (2019)
    359940,  # Three Billboards Outside Ebbing, Missouri (2018)
    376867,  # Moonlight (2017)
    281957,  # The Revenant (2016)
    85350,   # Boyhood (2015)
    76203,   # 12 Years a Slave (2014)
    68734,   # Argo (2013)
    65057,   # The Descendants (2012)
    37799,   # The Social Network (2011)
    19995,   # Avatar (2010)
    12405,   # Slumdog Millionaire (2009)
    4347,    # Atonement (2008)
    1164,    # Babel (2007)
    142,     # Brokeback Mountain (2006)
    2567,    # The Aviator (2005)
    122,     # The Lord of the Rings: The Return of the King (2004)
    13,      # Forrest Gump (1995)
    279,     # Amadeus (1985)
    826,     # The Bridge on the River Kwai (1958)
    2897,    # Around the World in 80 Days (1957)
    220,     # East of Eden (1956)
    654,     # On the Waterfront (1955)
    29912,   # The Robe (1954)
    27191,   # The Greatest Show on Earth (1953)
    25673,   # A Place in the Sun (1952)
}

GOLDEN_GLOBE_DRAMA_NOM_TMDB_IDS: set[int] = {
    # 2026 (83rd)
    1062722,  # Frankenstein
    1456349,  # It Was Just an Accident
    1220564,  # The Secret Agent
    1124566,  # Sentimental Value
    1233413,  # Sinners
    # 2025 (82nd)
    661539,   # A Complete Unknown
    974576,   # Conclave
    693134,   # Dune: Part Two
    1028196,  # Nickel Boys
    1211472,  # September 5
    # 2024 (81st)
    915935,   # Anatomy of a Fall
    466420,   # Killers of the Flower Moon
    523607,   # Maestro
    666277,   # Past Lives
    467244,   # The Zone of Interest
    # 2023 (80th)
    76600,    # Avatar: The Way of Water
    614934,   # Elvis
    817758,   # TÁR
    361743,   # Top Gun: Maverick
    # 2022 (79th)
    777270,   # Belfast
    776503,   # CODA
    438631,   # Dune
    614917,   # King Richard
    # 2021 (78th)
    600354,   # The Father
    614560,   # Mank
    582014,   # Promising Young Woman
    556984,   # The Trial of the Chicago 7
    # 2020 (77th)
    398978,   # The Irishman
    475557,   # Joker
    492188,   # Marriage Story
    551332,   # The Two Popes
    # 2019 (76th)
    284054,   # Black Panther
    487558,   # BlacKkKlansman
    465914,   # If Beale Street Could Talk
    332562,   # A Star Is Born
    # 2018 (75th)
    398818,   # Call Me by Your Name
    374720,   # Dunkirk
    446354,   # The Post
    399055,   # The Shape of Water
    # 2017 (74th)
    324786,   # Hacksaw Ridge
    338766,   # Hell or High Water
    334543,   # Lion
    334541,   # Manchester by the Sea
    # 2016 (73rd)
    258480,   # Carol
    76341,    # Mad Max: Fury Road
    264644,   # Room
    314365,   # Spotlight
    # 2015 (72nd)
    87492,    # Foxcatcher
    205596,   # The Imitation Game
    273895,   # Selma
    266856,   # The Theory of Everything
    # 2014 (71st)
    109424,   # Captain Phillips
    49047,    # Gravity
    205220,   # Philomena
    96721,    # Rush
    # 2013 (70th)
    68718,    # Django Unchained
    87827,    # Life of Pi
    72976,    # Lincoln
    97630,    # Zero Dark Thirty
    # 2012 (69th)
    50014,    # The Help
    44826,    # Hugo
    10316,    # The Ides of March
    60308,    # Moneyball
    57212,    # War Horse
    # 2011 (68th)
    44214,    # Black Swan
    45317,    # The Fighter
    27205,    # Inception
    45269,    # The King's Speech
    # 2010 (67th)
    12162,    # The Hurt Locker
    16869,    # Inglourious Basterds
    25793,    # Precious
    22947,    # Up in the Air
    # 2009 (66th)
    4922,     # The Curious Case of Benjamin Button
    11499,    # Frost/Nixon
    8055,     # The Reader
    4148,     # Revolutionary Road
    # 2008 (65th)
    4982,     # American Gangster
    2252,     # Eastern Promises
    14047,    # The Great Debaters
    4566,     # Michael Clayton
    6977,     # No Country for Old Men
    7345,     # There Will Be Blood
    # 2007 (64th)
    10741,    # Bobby
    1422,     # The Departed
    1440,     # Little Children
    1165,     # The Queen
    # 2006 (63rd)
    1985,     # The Constant Gardener
    3291,     # Good Night, and Good Luck.
    59,       # A History of Violence
    116,      # Match Point
}

# ---------------------------------------------------------------------------
# Golden Globe Best Motion Picture – Musical or Comedy — hardcoded TMDB IDs
# Sourced from: themoviedb.org/award/4-the-golden-globe-awards/category/8
# Winners: all years available. Nominees: 2006 onwards.
# ---------------------------------------------------------------------------

GOLDEN_GLOBE_COMEDY_WINNER_TMDB_IDS: set[int] = {
    1054867,  # One Battle After Another (2026)
    974950,   # Emilia Pérez (2025)
    792307,   # Poor Things (2024)
    674324,   # The Banshees of Inisherin (2023)
    511809,   # West Side Story (2022)
    740985,   # Borat Subsequent Moviefilm (2021)
    466272,   # Once Upon a Time... in Hollywood (2020)
    490132,   # Green Book (2019)
    391713,   # Lady Bird (2018)
    313369,   # La La Land (2017)
    286217,   # The Martian (2016)
    120467,   # The Grand Budapest Hotel (2015)
    168672,   # American Hustle (2014)
    82695,    # Les Misérables (2013)
    74643,    # The Artist (2012)
    39781,    # The Kids Are All Right (2011)
    18785,    # The Hangover (2010)
    5038,     # Vicky Cristina Barcelona (2009)
    13885,    # Sweeney Todd (2008)
    1125,     # Dreamgirls (2007)
    69,       # Walk the Line (2006)
    9675,     # Sideways (2005)
    153,      # Lost in Translation (2004)
    8587,     # The Lion King (1995)
    9326,     # Romancing the Stone (1985)
    16520,    # The King and I (1957)
    4825,     # Guys and Dolls (1956)
    51044,    # Carmen Jones (1955)
    65787,    # With a Song in My Heart (1953)
    2769,     # An American in Paris (1952)
}

GOLDEN_GLOBE_COMEDY_NOM_TMDB_IDS: set[int] = {
    # 2026 (83rd)
    1299655,  # Blue Moon
    701387,   # Bugonia
    1317288,  # Marty Supreme
    639988,   # No Other Choice
    1254808,  # Nouvelle Vague
    # 2025 (82nd)
    1013850,  # A Real Pain
    1064213,  # Anora
    937287,   # Challengers
    933260,   # The Substance
    402431,   # Wicked
    # 2024 (81st)
    964980,   # Air
    1056360,  # American Fiction
    346698,   # Barbie
    840430,   # The Holdovers
    839369,   # May December
    # 2023 (80th)
    615777,   # Babylon
    545611,   # Everything Everywhere All at Once
    661374,   # Glass Onion: A Knives Out Mystery
    497828,   # Triangle of Sadness
    # 2022 (79th)
    730047,   # Cyrano
    646380,   # Don't Look Up
    718032,   # Licorice Pizza
    537116,   # tick, tick... BOOM!
    # 2021 (78th)
    556574,   # Hamilton
    586101,   # Music
    587792,   # Palm Springs
    611213,   # The Prom
    # 2020 (77th)
    528888,   # Dolemite Is My Name
    515001,   # Jojo Rabbit
    546554,   # Knives Out
    504608,   # Rocketman
    # 2019 (76th)
    455207,   # Crazy Rich Asians
    375262,   # The Favourite
    400650,   # Mary Poppins Returns
    429197,   # Vice
    # 2018 (75th)
    371638,   # The Disaster Artist
    419430,   # Get Out
    316029,   # The Greatest Showman
    389015,   # I, Tonya
    # 2017 (74th)
    342737,   # 20th Century Women
    293660,   # Deadpool
    315664,   # Florence Foster Jenkins
    369557,   # Sing Street
    # 2016 (73rd)
    318846,   # The Big Short
    274479,   # Joy
    238713,   # Spy
    271718,   # Trainwreck
    # 2015 (72nd)
    194662,   # Birdman
    224141,   # Into the Woods
    234200,   # Pride
    239563,   # St. Vincent
    # 2014 (71st)
    152601,   # Her
    86829,    # Inside Llewyn Davis
    129670,   # Nebraska
    106646,   # The Wolf of Wall Street
    # 2013 (70th)
    74534,    # The Best Exotic Marigold Hotel
    83666,    # Moonrise Kingdom
    81025,    # Salmon Fishing in the Yemen
    82693,    # Silver Linings Playbook
    # 2012 (69th)
    40807,    # 50/50
    55721,    # Bridesmaids
    59436,    # Midnight in Paris
    75900,    # My Week with Marilyn
    # 2011 (68th)
    12155,    # Alice in Wonderland
    42297,    # Burlesque
    39514,    # RED
    37710,    # The Tourist
    # 2010 (67th)
    19913,    # (500) Days of Summer
    22897,    # It's Complicated
    24803,    # Julie & Julia
    10197,    # Nine
    # 2009 (66th)
    4944,     # Burn After Reading
    10503,    # Happy-Go-Lucky
    8321,     # In Bruges
    11631,    # Mamma Mia!
    # 2008 (65th)
    4688,     # Across the Universe
    6538,     # Charlie Wilson's War
    2976,     # Hairspray
    7326,     # Juno
    # 2007 (64th)
    496,      # Borat: Cultural Learnings of America
    350,      # The Devil Wears Prada
    773,      # Little Miss Sunshine
    9388,     # Thank You for Smoking
    # 2006 (63rd)
    10773,    # Mrs. Henderson Presents
    4348,     # Pride & Prejudice
    9899,     # The Producers
    10707,    # The Squid and the Whale
}

# ---------------------------------------------------------------------------
# Golden Globe Best Television Series – Drama — hardcoded TMDB IDs
# Sourced from: themoviedb.org/award/4-the-golden-globe-awards/category/42
# Winners: all years available. Nominees: 2006 onwards.
# ---------------------------------------------------------------------------

GOLDEN_GLOBE_TV_DRAMA_WINNER_TMDB_IDS: set[int] = {
    250307,   # The Pitt (2026)
    126308,   # Shōgun (2025)
    76331,    # Succession (2024 & 2022 & 2020)
    94997,    # House of the Dragon (2023)
    65494,    # The Crown (2021 & 2017)
    46533,    # The Americans (2019)
    69478,    # The Handmaid's Tale (2018)
    62560,    # Mr. Robot (2016)
    61463,    # The Affair (2015)
    1396,     # Breaking Bad (2014)
    1407,     # Homeland (2013 & 2012)
    1621,     # Boardwalk Empire (2011)
    1104,     # Mad Men (2010 & 2009 & 2008)
    1416,     # Grey's Anatomy (2007)
    4607,     # Lost (2006)
    3750,     # Nip/Tuck (2005)
}

GOLDEN_GLOBE_TV_DRAMA_NOM_TMDB_IDS: set[int] = {
    # 2026 (83rd)
    203857,   # The Diplomat
    225171,   # Pluribus
    95396,    # Severance
    95480,    # Slow Horses
    111803,   # The White Lotus
    # 2025 (82nd)
    222766,   # The Day of the Jackal
    203857,   # The Diplomat
    118642,   # Mr. & Mrs. Smith
    95480,    # Slow Horses
    93405,    # Squid Game
    # 2024 (81st)
    157744,   # 1923
    65494,    # The Crown
    203857,   # The Diplomat
    100088,   # The Last of Us
    90282,    # The Morning Show
    # 2023 (80th)
    60059,    # Better Call Saul
    65494,    # The Crown
    69740,    # Ozark
    95396,    # Severance
    # 2022 (79th)
    96677,    # Lupin
    90282,    # The Morning Show
    79084,    # POSE
    93405,    # Squid Game
    # 2021 (78th)
    82816,    # Lovecraft Country
    82856,    # The Mandalorian
    69740,    # Ozark
    81354,    # Ratched
    # 2020 (77th)
    66292,    # Big Little Lies
    65494,    # The Crown
    72750,    # Killing Eve
    90282,    # The Morning Show
    # 2019 (76th)
    80307,    # Bodyguard
    80335,    # Homecoming
    72750,    # Killing Eve
    79084,    # POSE
    # 2018 (75th)
    65494,    # The Crown
    1399,     # Game of Thrones
    66732,    # Stranger Things
    67136,    # This Is Us
    # 2017 (74th)
    1399,     # Game of Thrones
    66732,    # Stranger Things
    67136,    # This Is Us
    63247,    # Westworld
    # 2016 (73rd)
    61733,    # Empire
    1399,     # Game of Thrones
    63351,    # Narcos
    56570,    # Outlander
    # 2015 (72nd)
    33907,    # Downton Abbey
    1399,     # Game of Thrones
    1435,     # The Good Wife
    1425,     # House of Cards
    # 2014 (71st)
    33907,    # Downton Abbey
    1435,     # The Good Wife
    1425,     # House of Cards
    58937,    # Masters of Sex
    # 2013 (70th)
    1396,     # Breaking Bad
    1621,     # Boardwalk Empire
    33907,    # Downton Abbey
    15621,    # The Newsroom
    # 2012 (69th)
    1413,     # American Horror Story
    1621,     # Boardwalk Empire
    38922,    # Boss
    1399,     # Game of Thrones
    # 2011 (68th)
    1405,     # Dexter
    1435,     # The Good Wife
    1104,     # Mad Men
    1402,     # The Walking Dead
    # 2010 (67th)
    4392,     # Big Love
    1405,     # Dexter
    1408,     # House
    10545,    # True Blood
    # 2009 (66th)
    1405,     # Dexter
    1408,     # House
    14069,    # In Treatment
    10545,    # True Blood
    # 2008 (65th)
    4392,     # Big Love
    4920,     # Damages
    1416,     # Grey's Anatomy
    1408,     # House
    2942,     # The Tudors
    # 2007 (64th)
    1973,     # 24
    4392,     # Big Love
    1639,     # Heroes
    4607,     # Lost
    # 2006 (63rd)
    4015,     # Commander in Chief
    1416,     # Grey's Anatomy
    2288,     # Prison Break
    1891,     # Rome
}

# ---------------------------------------------------------------------------
# Golden Globe Best Television Series – Musical or Comedy — hardcoded TMDB IDs
# Sourced from: themoviedb.org/award/4-the-golden-globe-awards/category/43
# Winners: all years available. Nominees: 2006 onwards.
# ---------------------------------------------------------------------------

GOLDEN_GLOBE_TV_COMEDY_WINNER_TMDB_IDS: set[int] = {
    247767,   # The Studio (2026)
    124101,   # Hacks (2025 & 2022)
    136315,   # The Bear (2024)
    125935,   # Abbott Elementary (2023)
    61662,    # Schitt's Creek (2021)
    67070,    # Fleabag (2020)
    81290,    # The Kominsky Method (2019)
    70796,    # The Marvelous Mrs. Maisel (2018)
    65495,    # Atlanta (2017)
    61744,    # Mozart in the Jungle (2016)
    61406,    # Transparent (2015)
    48891,    # Brooklyn Nine-Nine (2014)
    42282,    # Girls (2013)
    1421,     # Modern Family (2012)
    1417,     # Glee (2011 & 2010)
    4608,     # 30 Rock (2009)
    2693,     # Extras (2008)
    4626,     # Ugly Betty (2007)
    693,      # Desperate Housewives (2006 & 2005)
}

GOLDEN_GLOBE_TV_COMEDY_NOM_TMDB_IDS: set[int] = {
    # 2026 (83rd)
    125935,   # Abbott Elementary
    124101,   # Hacks
    250923,   # Nobody Wants This
    107113,   # Only Murders in the Building
    136315,   # The Bear
    # 2025 (82nd)
    125935,   # Abbott Elementary
    136315,   # The Bear
    236235,   # The Gentlemen
    250923,   # Nobody Wants This
    107113,   # Only Murders in the Building
    # 2024 (81st)
    125935,   # Abbott Elementary
    73107,    # Barry
    222023,   # Jury Duty
    107113,   # Only Murders in the Building
    97546,    # Ted Lasso
    # 2023 (80th)
    136315,   # The Bear
    124101,   # Hacks
    107113,   # Only Murders in the Building
    119051,   # Wednesday
    # 2022 (79th)
    93812,    # The Great
    107113,   # Only Murders in the Building
    95215,    # Reservation Dogs
    97546,    # Ted Lasso
    # 2021 (78th)
    82596,    # Emily in Paris
    93287,    # The Flight Attendant
    93812,    # The Great
    97546,    # Ted Lasso
    # 2020 (77th)
    73107,    # Barry
    81290,    # The Kominsky Method
    70796,    # The Marvelous Mrs. Maisel
    83127,    # The Politician
    # 2019 (76th)
    73107,    # Barry
    66573,    # The Good Place
    73925,    # Kidding
    70796,    # The Marvelous Mrs. Maisel
    # 2018 (75th)
    61381,    # black-ish
    64254,    # Master of None
    71733,    # SMILF
    74321,    # Will & Grace
    # 2017 (74th)
    61381,    # black-ish
    61744,    # Mozart in the Jungle
    61406,    # Transparent
    2947,     # Veep
    # 2016 (73rd)
    64043,    # Casual
    1424,     # Orange Is the New Black
    60573,    # Silicon Valley
    61406,    # Transparent
    2947,     # Veep
    # 2015 (72nd)
    42282,    # Girls
    61418,    # Jane the Virgin
    1424,     # Orange Is the New Black
    60573,    # Silicon Valley
    # 2014 (71st)
    1418,     # The Big Bang Theory
    42282,    # Girls
    1421,     # Modern Family
    8592,     # Parks and Recreation
    # 2013 (70th)
    1418,     # The Big Bang Theory
    31841,    # Episodes
    1421,     # Modern Family
    39325,    # Smash
    # 2012 (69th)
    34594,    # Enlightened
    31841,    # Episodes
    1417,     # Glee
    1420,     # New Girl
    # 2011 (68th)
    4608,     # 30 Rock
    1418,     # The Big Bang Theory
    32406,    # The Big C
    1421,     # Modern Family
    18053,    # Nurse Jackie
    # 2010 (67th)
    4608,     # 30 Rock
    1940,     # Entourage
    1421,     # Modern Family
    2316,     # The Office
    # 2009 (66th)
    1215,     # Californication
    1940,     # Entourage
    2316,     # The Office
    186,      # Weeds
    # 2008 (65th)
    4608,     # 30 Rock
    1215,     # Californication
    1940,     # Entourage
    5639,     # Pushing Daisies
    # 2007 (64th)
    693,      # Desperate Housewives
    1940,     # Entourage
    2316,     # The Office
    186,      # Weeds
    # 2006 (63rd)
    4546,     # Curb Your Enthusiasm
    1940,     # Entourage
    252,      # Everybody Hates Chris
    2317,     # My Name Is Earl
    186,      # Weeds
}

# ---------------------------------------------------------------------------
# Golden Globe Best Television Limited/Anthology Series — hardcoded TMDB IDs
# Sourced from: themoviedb.org/award/4-the-golden-globe-awards/category/44
# Winners: all years available. Nominees: 2006 onwards.
# ---------------------------------------------------------------------------

GOLDEN_GLOBE_TV_LIMITED_WINNER_TMDB_IDS: set[int] = {
    249042,   # Adolescence (2026)
    241259,   # Baby Reindeer (2025)
    154385,   # BEEF (2024)
    111803,   # The White Lotus (2023)
    80039,    # The Underground Railroad (2022)
    87739,    # The Queen's Gambit (2021)
    87108,    # Chernobyl (2020)
    64513,    # American Crime Story (2019 & 2017)
    66292,    # Big Little Lies (2018)
    61697,    # Wolf Hall (2016)
    41693,    # Carlos (2011)
    15114,    # John Adams (2009)
    13291,    # Elizabeth I (2007)
    14968,    # Empire Falls (2006)
}

GOLDEN_GLOBE_TV_LIMITED_NOM_TMDB_IDS: set[int] = {
    # 2026 (83rd)
    246386,   # All Her Fault
    241405,   # Dying for Sex
    250504,   # The Beast in Me
    253376,   # The Girlfriend
    42009,    # Black Mirror
    # 2025 (82nd)
    147050,   # Disclaimer
    225634,   # Monsters: The Lyle and Erik Menendez Story
    194764,   # The Penguin
    94028,    # RIPLEY
    46648,    # True Detective
    # 2024 (81st)
    155421,   # All the Light We Cannot See
    95555,    # Daisy Jones & the Six
    60622,    # Fargo
    216089,   # Fellow Travelers
    117303,   # Lessons in Chemistry
    # 2023 (80th)
    155537,   # Black Bird
    113988,   # DAHMER - Monster: The Jeffrey Dahmer Story
    122066,   # The Dropout
    114925,   # Pam & Tommy
    # 2022 (79th)
    110695,   # Dopesick
    64513,    # American Crime Story
    111141,   # Maid
    115004,   # Mare of Easttown
    # 2021 (78th)
    89905,    # Normal People
    90705,    # Small Axe
    83851,    # The Undoing
    99581,    # Unorthodox
    # 2020 (77th)
    82744,    # Catch-22
    81131,    # Fosse/Verdon
    80443,    # The Loudest Voice
    91275,    # Unbelievable
    # 2019 (76th)
    71769,    # The Alienist
    72039,    # Escape at Dannemora
    70453,    # Sharp Objects
    79299,    # A Very English Scandal
    # 2018 (75th)
    60622,    # Fargo
    69851,    # FEUD
    39852,    # The Sinner
    46638,    # Top of the Lake
    # 2017 (74th)
    60791,    # American Crime
    61859,    # The Night Manager
    66276,    # The Night Of
    # 2016 (73rd)
    60791,    # American Crime
    1413,     # American Horror Story
    60622,    # Fargo
    62516,    # Flesh and Bone
    # 2011 (68th)
    16997,    # The Pacific
    33234,    # The Pillars of the Earth
    # 2007 (64th)
    2489,     # Bleak House
    20056,    # Broken Trail
    # 2006 (63rd)
    11099,    # Into the West
}

# ---------------------------------------------------------------------------
# Emmy Outstanding Drama / Comedy / Limited Series — nominees only
# Sourced from: themoviedb.org/award/82-emmy-awards (categories 1, 2, 3)
# Replaces the generic emmy-award-nominated keyword which captures all Emmy
# nominations across every category (acting, directing, writing, etc.).
# Winners are already captured in EMMY_WINNER_TMDB_IDS above.
# Update annually after the Emmy ceremony.
# ---------------------------------------------------------------------------

EMMY_DRAMA_NOM_TMDB_IDS: set[int] = {
    # 2026 (78th)
    203857,   # The Diplomat
    81723,    # The Gilded Age
    224372,   # A Knight of the Seven Kingdoms
    245927,   # Paradise
    225171,   # Pluribus
    95480,    # Slow Horses
    241609,   # Your Friends & Neighbors
    # 2025 (77th)
    83867,    # Star Wars: Andor
    203857,   # The Diplomat
    100088,   # The Last of Us
    245927,   # Paradise
    95396,    # Severance
    95480,    # Slow Horses
    111803,   # The White Lotus
    # 2024 (76th)
    65494,    # The Crown
    106379,   # Fallout
    81723,    # The Gilded Age
    90282,    # The Morning Show
    118642,   # Mr. & Mrs. Smith
    108545,   # 3 Body Problem
    # 2023 (75th)
    60059,    # Better Call Saul
    94997,    # House of the Dragon
    117488,   # Yellowjackets
    # 2022 (74th)
    85552,    # Euphoria
    69740,    # Ozark
    93405,    # Squid Game
    66732,    # Stranger Things
    # 2021 (73rd)
    91239,    # Bridgerton
    82816,    # Lovecraft Country
    79084,    # POSE
    76479,    # The Boys
    82856,    # The Mandalorian
    67136,    # This Is Us
    # 2020 (72nd)
    72750,    # Killing Eve
    # 2019 (71st)
    80307,    # Bodyguard
    72750,    # Killing Eve
    69740,    # Ozark
    79084,    # POSE
    76331,    # Succession
    # 2018 (70th)
    67136,    # This Is Us
    66732,    # Stranger Things
    46533,    # The Americans
    63247,    # Westworld
    # 2017 (69th)
    67136,    # This Is Us
    60059,    # Better Call Saul
    1425,     # House of Cards
    66732,    # Stranger Things
    63247,    # Westworld
    # 2016 (68th)
    60059,    # Better Call Saul
    33907,    # Downton Abbey
    1425,     # House of Cards
    62560,    # Mr. Robot
    46533,    # The Americans
    # 2015 (67th)
    60059,    # Better Call Saul
    33907,    # Downton Abbey
    1425,     # House of Cards
    1104,     # Mad Men
    1424,     # Orange Is the New Black
    # 2014 (66th)
    33907,    # Downton Abbey
    1425,     # House of Cards
    1104,     # Mad Men
    46648,    # True Detective
    # 2013 (65th)
    33907,    # Downton Abbey
    1425,     # House of Cards
    1104,     # Mad Men
    # 2012 (64th)
    1621,     # Boardwalk Empire
    33907,    # Downton Abbey
    1104,     # Mad Men
    # 2011 (63rd)
    1621,     # Boardwalk Empire
    1405,     # Dexter
    4278,     # Friday Night Lights
}

EMMY_COMEDY_NOM_TMDB_IDS: set[int] = {
    # 2026 (78th)
    125935,   # Abbott Elementary
    136315,   # The Bear
    124101,   # Hacks
    245318,   # Margo's Got Money Troubles
    250923,   # Nobody Wants This
    107113,   # Only Murders in the Building
    136311,   # Shrinking
    # 2025 (77th)
    125935,   # Abbott Elementary
    136315,   # The Bear
    124101,   # Hacks
    250923,   # Nobody Wants This
    107113,   # Only Murders in the Building
    136311,   # Shrinking
    83631,    # What We Do in the Shadows
    # 2024 (76th)
    125935,   # Abbott Elementary
    136315,   # The Bear
    4546,     # Curb Your Enthusiasm
    107113,   # Only Murders in the Building
    157367,   # Palm Royale
    95215,    # Reservation Dogs
    83631,    # What We Do in the Shadows
    # 2023 (75th)
    125935,   # Abbott Elementary
    73107,    # Barry
    222023,   # Jury Duty
    70796,    # The Marvelous Mrs. Maisel
    107113,   # Only Murders in the Building
    97546,    # Ted Lasso
    119051,   # Wednesday
    # 2022 (74th)
    125935,   # Abbott Elementary
    73107,    # Barry
    4546,     # Curb Your Enthusiasm
    124101,   # Hacks
    70796,    # The Marvelous Mrs. Maisel
    107113,   # Only Murders in the Building
    83631,    # What We Do in the Shadows
    # 2021 (73rd)
    77169,    # Cobra Kai
    82596,    # Emily in Paris
    85702,    # PEN15
    93287,    # The Flight Attendant
    81290,    # The Kominsky Method
    61381,    # black-ish
    # 2020 (72nd)
    4546,     # Curb Your Enthusiasm
    81357,    # Dead to Me
    67883,    # Insecure
    66573,    # The Good Place
    81290,    # The Kominsky Method
    70796,    # The Marvelous Mrs. Maisel
    83631,    # What We Do in the Shadows
    # 2019 (71st)
    73107,    # Barry
    84977,    # Russian Doll
    61662,    # Schitt's Creek
    66573,    # The Good Place
    70796,    # The Marvelous Mrs. Maisel
    2947,     # Veep
    # 2018 (70th)
    65495,    # Atlanta
    73107,    # Barry
    4546,     # Curb Your Enthusiasm
    70573,    # GLOW
    60573,    # Silicon Valley
    61671,    # Unbreakable Kimmy Schmidt
    61381,    # black-ish
    # 2017 (69th)
    65495,    # Atlanta
    64254,    # Master of None
    1421,     # Modern Family
    60573,    # Silicon Valley
    61671,    # Unbreakable Kimmy Schmidt
    61381,    # black-ish
    # 2016 (68th)
    64254,    # Master of None
    1421,     # Modern Family
    60573,    # Silicon Valley
    61406,    # Transparent
    61671,    # Unbreakable Kimmy Schmidt
    61381,    # black-ish
    # 2015 (67th)
    32962,    # Louie
    1421,     # Modern Family
    8592,     # Parks and Recreation
    60573,    # Silicon Valley
    61406,    # Transparent
    61671,    # Unbreakable Kimmy Schmidt
    # 2014 (66th)
    32962,    # Louie
    1424,     # Orange Is the New Black
    60573,    # Silicon Valley
    1418,     # The Big Bang Theory
    # 2013 (65th)
    4608,     # 30 Rock
    42282,    # Girls
    32962,    # Louie
    1418,     # The Big Bang Theory
    2947,     # Veep
    # 2012 (64th)
    4608,     # 30 Rock
    4546,     # Curb Your Enthusiasm
    42282,    # Girls
    1418,     # The Big Bang Theory
    2947,     # Veep
    # 2011 (63rd)
    4608,     # 30 Rock
    1417,     # Glee
    8592,     # Parks and Recreation
    1418,     # The Big Bang Theory
    2316,     # The Office
}

EMMY_LIMITED_NOM_TMDB_IDS: set[int] = {
    # 2026 (78th)
    246386,   # All Her Fault
    250504,   # The Beast in Me
    154385,   # Beef
    131142,   # Love Story: John F. Kennedy Jr. & Carolyn Bessette
    # 2025 (77th)
    42009,    # Black Mirror
    241405,   # Dying for Sex
    225634,   # Monsters: The Lyle and Erik Menendez Story
    194764,   # The Penguin
    # 2024 (76th)
    60622,    # Fargo
    117303,   # Lessons in Chemistry
    94028,    # RIPLEY
    46648,    # True Detective
    # 2023 (75th)
    113988,   # DAHMER - Monster: The Jeffrey Dahmer Story
    95555,    # Daisy Jones & the Six
    156401,   # Fleishman Is in Trouble
    92830,    # Obi-Wan Kenobi
    # 2022 (74th)
    110695,   # Dopesick
    122066,   # The Dropout
    95665,    # Inventing Anna
    114925,   # Pam & Tommy
    # 2021 (73rd)
    102619,   # I May Destroy You
    115004,   # Mare of Easttown
    80039,    # The Underground Railroad
    85271,    # WandaVision
    # 2020 (72nd)
    90257,    # Little Fires Everywhere
    83605,    # Mrs. America
    91275,    # Unbelievable
    99581,    # Unorthodox
    # 2019 (71st)
    72039,    # Escape at Dannemora
    81131,    # Fosse/Verdon
    70453,    # Sharp Objects
    81355,    # When They See Us
    # 2018 (70th)
    70128,    # Genius
    73467,    # Godless
    72787,    # Patrick Melrose
    71769,    # The Alienist
    # 2017 (69th)
    69851,    # FEUD
    66276,    # The Night Of
    # 2016 (68th)
    60791,    # American Crime
    66606,    # Roots
    61859,    # The Night Manager
    # 2015 (67th)
    1413,     # American Horror Story
    61123,    # The Honourable Woman
    # 2014 (66th)
    62829,    # Bonnie & Clyde
    1426,     # Luther
    57092,    # The White Queen
    17967,    # Treme
    # 2010 (62nd)
    14264,    # Cranford
    # 2009 (61st)
    17035,    # Generation Kill
    # 2008 (60th)
    19997,    # The Andromeda Strain
    12584,    # Tin Man
    # 2007 (59th)
    5900,     # The Starter Wife
    2583,     # Prime Suspect
    # 2006 (58th)
    2489,     # Bleak House
    11099,    # Into the West
    2409,     # Sleeper Cell
    # 2004 (56th)
    22859,    # American Family
    814,      # Hornblower
    13261,    # Traffic
    # 2003 (55th)
    46976,    # Hitler: The Rise of Evil
    2701,     # Napoleon
    # 2002 (54th)
    73536,    # Dinotopia
    13729,    # Shackleton
    5256,     # The Mists of Avalon
    # 2001 (53rd)
    46679,    # Further Tales of the City
    75577,    # Life with Judy Garland: Me and My Shadows
    7389,     # Nuremberg
    # 2000 (52nd)
    20658,    # Arabian Nights
    47059,    # Jesus
    37542,    # The Beach Boys: An American Family
    47058,    # P.T. Barnum
    # 1999 (51st)
    286423,   # Great Expectations
    16133,    # Joan of Arc
    303676,   # The '60s
    9290,     # The Temptations
}

# ---------------------------------------------------------------------------
# Combined lookup sets — used in parse_mdblist_awards for O(1) checks
# ---------------------------------------------------------------------------

# TMDB movie and TV ids are separate namespaces — movie/105 is Back to the
# Future, tv/105 is Sex and the City — so the film and series sets must never
# be searched together.  They are kept apart here and picked by media type.
_GG_FILM_WINNERS: set[int] = (
    GOLDEN_GLOBE_DRAMA_WINNER_TMDB_IDS
    | GOLDEN_GLOBE_COMEDY_WINNER_TMDB_IDS
)

_GG_FILM_NOMS: set[int] = (
    GOLDEN_GLOBE_DRAMA_NOM_TMDB_IDS
    | GOLDEN_GLOBE_COMEDY_NOM_TMDB_IDS
)

_GG_TV_WINNERS: set[int] = (
    GOLDEN_GLOBE_TV_DRAMA_WINNER_TMDB_IDS
    | GOLDEN_GLOBE_TV_COMEDY_WINNER_TMDB_IDS
    | GOLDEN_GLOBE_TV_LIMITED_WINNER_TMDB_IDS
)

_GG_TV_NOMS: set[int] = (
    GOLDEN_GLOBE_TV_DRAMA_NOM_TMDB_IDS
    | GOLDEN_GLOBE_TV_COMEDY_NOM_TMDB_IDS
    | GOLDEN_GLOBE_TV_LIMITED_NOM_TMDB_IDS
)

_EMMY_ALL_NOMS: set[int] = (
    EMMY_DRAMA_NOM_TMDB_IDS
    | EMMY_COMEDY_NOM_TMDB_IDS
    | EMMY_LIMITED_NOM_TMDB_IDS
)

# ---------------------------------------------------------------------------
# Emmy winners — hardcoded TMDB IDs
# Drama, Comedy and Limited Series winners only.
# ---------------------------------------------------------------------------

EMMY_WINNER_TMDB_IDS: set[int] = {
    # Comedy
    270476,  # Widow's Bay
    247767,  # The Studio
    124101,  # Hacks
    136315,  # The Bear
    97546,   # Ted Lasso
    61662,   # Schitt's Creek
    67070,   # Fleabag
    70796,   # The Marvelous Mrs Maisel
    2947,    # Veep
    1421,    # Modern Family
    4608,    # 30 Rock
    2316,    # The Office
    2140,    # Everybody Loves Raymond
    4589,    # Arrested Development
    1668,    # Friends
    105,     # Sex and the City
    4454,    # Will & Grace
    1480,    # Ally McBeal
    3452,    # Frasier
    1400,    # Seinfeld
    3219,    # Murphy Brown
    141,     # Cheers
    4500,    # The Wonder Years
    1678,    # The Golden Girls
    1759,    # The Cosby Show
    3253,    # Barney Miller
    2251,    # Taxi
    1922,    # All in the Family
    2962,    # The Mary Tyler Moore Show
    918,     # M*A*S*H
    582,     # My World and Welcome to It
    # Drama
    250307,  # The Pitt
    126308,  # Shogun
    76331,   # Succession
    65494,   # The Crown
    1399,    # Game of Thrones
    69478,   # The Handmaid's Tale
    1396,    # Breaking Bad
    1407,    # Homeland
    1104,    # Mad Men
    1398,    # The Sopranos
    1973,    # 24
    4607,    # Lost
    688,     # The West Wing
    3050,    # The Practice
    549,     # Law & Order
    4588,    # ER
    194,     # NYPD Blue
    206,     # Picket Fences
    4396,    # Northern Exposure
    732,     # L.A. Law
    1448,    # thirtysomething
    4223,    # Cagney & Lacey
    3828,    # Hill Street Blues
    480,     # Lou Grant
    954,     # The Rockford Files
    492,     # Upstairs Downstairs
    9855,    # Police Story
    5021,    # The Waltons
    1103,    # Elizabeth R
    3213,    # Marcus Welby M.D.
    # Limited Series
    206828,  # DTF St. Louis
    249042,  # Adolescence
    154385,  # Beef
    111803,  # The White Lotus
    87739,   # The Queen's Gambit
    79788,   # Watchmen
    87108,   # Chernobyl
    64513,   # American Crime Story
    66292,   # Big Little Lies
    61585,   # Olive Kitteridge
    60622,   # Fargo
    33907,   # Downton Abbey
    16997,   # The Pacific
    13561,   # Little Dorrit
    15114,   # John Adams
    20056,   # Broken Trail
    13291,   # Elizabeth I
    13688,   # The Lost Prince
    11245,   # Angels in America
    2432,    # Taken
    4613,    # Band of Brothers
    21276,   # Anne Frank: The Whole Story
    20658,   # Arabian Nights
    814,     # Hornblower
    3556,    # From the Earth to the Moon
    11121,   # The Odyssey
    13675,   # Gulliver's Travels
}


# ---------------------------------------------------------------------------
# Award parsing from MDblist keywords
# ---------------------------------------------------------------------------

# Labels derived purely from the TMDB id (see tmdb_id_awards).  They are
# re-derived on every cache read, so the stored copies are never trusted.
_ID_DERIVED_LABELS = frozenset({
    "Globe Winner", "Globe Nominee", "Emmy Winner", "Emmy Nominee",
})


def tmdb_id_awards(
    tmdb_id: int | str | None,
    media_type: str | None,
) -> tuple[list[str], list[str]]:
    """Golden Globe and Emmy wins / noms for a TMDB id, in its own namespace.

    Movies are checked against the film Globe categories only; series against
    the TV Globe categories and the Emmys.  An unknown media type is treated as
    a movie, the /poster default.
    """
    try:
        numeric = int(tmdb_id) if tmdb_id is not None else None
    except (ValueError, TypeError):
        numeric = None
    if numeric is None:
        return [], []

    wins: list[str] = []
    noms: list[str] = []
    is_tv = media_type in ("tv", "series")

    # --- Golden Globe ---
    gg_winners, gg_noms = (_GG_TV_WINNERS, _GG_TV_NOMS) if is_tv else (_GG_FILM_WINNERS, _GG_FILM_NOMS)
    if numeric in gg_winners:
        wins.append("Globe Winner")
    elif numeric in gg_noms:
        noms.append("Globe Nominee")

    # --- Emmy (television only) ---
    if is_tv:
        if numeric in EMMY_WINNER_TMDB_IDS:
            wins.append("Emmy Winner")
        elif numeric in _EMMY_ALL_NOMS:
            noms.append("Emmy Nominee")

    return wins, noms


def reconcile_cached_awards(
    wins: list[str],
    noms: list[str],
    tmdb_id: int | str | None,
    media_type: str | None,
) -> tuple[list[str], list[str]]:
    """Rebuild the id-derived labels in a cached award pair.

    Keyword-derived labels (the Oscars) are kept as stored — the keywords are
    not cached, so they cannot be re-checked.  Globe and Emmy labels are thrown
    away and re-derived from the id, so a row written before the namespaces
    were separated (or before a list was corrected) stops carrying the error.
    """
    id_wins, id_noms = tmdb_id_awards(tmdb_id, media_type)
    return (
        [w for w in wins if w not in _ID_DERIVED_LABELS] + id_wins,
        [n for n in noms if n not in _ID_DERIVED_LABELS] + id_noms,
    )


def parse_mdblist_awards(
    keywords: list[dict],
    tmdb_id: int | str | None = None,
    media_type: str | None = None,
) -> tuple[list[str], list[str]]:
    """
    Derive award wins and nominations from MDblist keyword objects.

    Best Picture wins/noms come from keywords:
        best-picture-winner   → win
        best-picture-nominated → nom

    Emmy wins come from the hardcoded EMMY_WINNER_TMDB_IDS set.
    Emmy noms come from EMMY_DRAMA/COMEDY/LIMITED_NOM_TMDB_IDS — Outstanding
    Series categories only, replacing the broad emmy-award-nominated keyword
    which fired on acting/directing/writing nominations too.

    Golden Globe wins/noms cover the top film categories for movies and the
    top TV categories for series — *media_type* picks the namespace, since
    TMDB movie and TV ids overlap (see tmdb_id_awards).

    Returns (wins, noms) where each is a list of human-readable strings.
    """
    keyword_names: set[str] = {
        (kw.get("name") or "").lower().strip()
        for kw in keywords
    }

    wins: list[str] = []
    noms: list[str] = []

    # --- Best Picture (Oscar) ---
    if "best-picture-winner" in keyword_names:
        wins.append("Oscar Winner")
    elif "best-picture-nominated" in keyword_names:
        noms.append("Oscar Nominee")

    # --- Golden Globe / Emmy — from the id, in its own namespace ---
    id_wins, id_noms = tmdb_id_awards(tmdb_id, media_type)
    wins.extend(id_wins)
    noms.extend(id_noms)

    return wins, noms


# ---------------------------------------------------------------------------
# Sash drawing
# ---------------------------------------------------------------------------

def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]

    try:
        ascent, descent = font.getmetrics()
    except AttributeError:
        ascent, descent = 0, 0

    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust

    return x, y


# Formerly used to auto-star awards whose winner and nominee shared the same
# label text ("Best Picture", "Golden Globe").  Those labels were renamed to
# "Oscar Winner"/"Oscar Nominee" and "Globe Winner"/"Globe Nominee" so the
# auto-star is no longer needed.  The set is kept empty for safety; the star
# prefix is now controlled by the sash_winner_star URL parameter instead.
_STAR_WIN_AWARDS: set[str] = set()


# Below this Value the source carries no reliable hue (see _frosted_tint); below
# this Saturation it is essentially white/grey. When the local region is either,
# a broader fallback region (typically the whole poster) is borrowed so the frost
# matches a real poster colour instead of going dark/neutral or washing out white.
_FROST_CONFIDENT_V = 0.22
# At/above this Saturation a cluster counts as a genuine colour (vs white/grey).
_FROST_CHROMATIC_S = 0.20
# A coloured cluster must cover at least this fraction of the region to be chosen
# over a white/grey majority. Small enough to catch modest colour accents (so the
# frost leans colour, not white), but above thin title text (e.g. the red "RUN" on
# an otherwise black-and-white poster) so that doesn't override an honest white.
_FROST_CHROMATIC_W = 0.08
# A cluster this dark carries no usable colour for a frosted element — it would
# only make the frost muddy — so it is skipped entirely.
_FROST_MIN_V = 0.16


def _is_skin_tone(r: float, g: float, b: float) -> bool:
    """Rough skin-tone test for faces (tan/beige/brown): warm with R>G>B and a
    moderate saturation. Deliberately excludes vivid reds and oranges so genuine
    poster colours aren't mistaken for skin. Skin is de-prioritised — but not
    banned — as a frost colour, since a face shouldn't drive the tint when the
    poster offers a real colour elsewhere."""
    import colorsys
    if not (r > g > b):
        return False
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return 0.015 <= h <= 0.11 and 0.20 <= s <= 0.68 and v >= 0.35


def _dominant_cluster(
    region: Image.Image,
) -> tuple[tuple[float, float, float] | None, float, float, bool]:
    """Most prominent *actual* colour of a region → ((r,g,b), value, saturation,
    is_skin).

    Quantises the region into a handful of real colour clusters, rather than
    taking a flat mean of every pixel — a mean of several distinct colours lands
    on a muddy grey/brown that appears nowhere in the art (the "invented colour"
    problem). Near-black clusters are skipped so the tint is never derived from
    shadows or letterboxing.

    Preference order among clusters that clear _FROST_CHROMATIC_S / _W:
      1. a genuine non-skin colour   (e.g. a teal background)
      2. a skin tone                 (only if no other colour qualifies)
    then, if nothing is chromatic, the best white/grey, then the brightest
    cluster. So white is only chosen when the region truly has no colour, and a
    face's skin only when the poster offers nothing else. Returns ``(None, 0, 0,
    False)`` only for an empty region; the trailing fields let the caller judge
    reliability and whether the pick was skin."""
    import colorsys
    if region.width == 0 or region.height == 0:
        return None, 0.0, 0.0, False
    # A modest downsample preserves the real colours; the old heavy-blur + 8x8
    # mean is exactly what smeared them into an invented average.
    small = region.convert("RGB")
    if max(small.size) > 64:
        # reducing_gap box-reduces a whole poster most of the way first, so
        # LANCZOS only finishes the last 2x (≤2/255 difference, a fraction of
        # the cost).
        small = small.resize((48, 48), Image.Resampling.LANCZOS, reducing_gap=2.0)
    try:
        q = small.quantize(colors=12, method=Image.Quantize.FASTOCTREE)
    except Exception:
        q = small.quantize(colors=12)
    palette = q.getpalette() or []
    counts  = q.getcolors() or []
    if not counts or not palette:
        arr = np.array(small, dtype=np.float32)
        rgb = (float(arr[:, :, 0].mean()), float(arr[:, :, 1].mean()), float(arr[:, :, 2].mean()))
        _hh, ss, vv = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        return rgb, vv, ss, _is_skin_tone(*rgb)

    total = float(sum(c for c, _ in counts)) or 1.0
    # best_colour: best non-skin chromatic cluster (preferred).
    # best_skin:   best skin-tone chromatic cluster (used only if no other colour).
    # best_any:    best cluster overall incl. white/grey (used if nothing chromatic).
    best_colour, best_colour_score, best_colour_hsv = None, -1.0, (0.0, 0.0)
    best_skin, best_skin_score, best_skin_hsv = None, -1.0, (0.0, 0.0)
    best_any, best_any_score, best_any_hsv = None, -1.0, (0.0, 0.0)
    brightest, brightest_hsv = None, (-1.0, 0.0)
    for count, idx in counts:
        r, g, b = palette[idx * 3:idx * 3 + 3]
        _h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v > brightest_hsv[0]:
            brightest_hsv, brightest = (v, s), (float(r), float(g), float(b))
        if v < _FROST_MIN_V:               # near-black — never a tint source
            continue
        weight = count / total
        # Population-led, biased toward *chroma* (saturation × value), not raw
        # saturation — otherwise a small, dark-but-saturated shadow (e.g. a deep
        # purple corner) outscores the poster's larger, brighter real palette.
        score = weight * (0.3 + s * v)
        rgb = (float(r), float(g), float(b))
        if score > best_any_score:
            best_any_score, best_any, best_any_hsv = score, rgb, (v, s)
        if s >= _FROST_CHROMATIC_S and weight >= _FROST_CHROMATIC_W:
            if _is_skin_tone(r, g, b):
                if score > best_skin_score:
                    best_skin_score, best_skin, best_skin_hsv = score, rgb, (v, s)
            elif score > best_colour_score:
                best_colour_score, best_colour, best_colour_hsv = score, rgb, (v, s)

    if best_colour is not None:
        return best_colour, best_colour_hsv[0], best_colour_hsv[1], False
    if best_skin is not None:
        return best_skin, best_skin_hsv[0], best_skin_hsv[1], True
    if best_any is not None:
        return best_any, best_any_hsv[0], best_any_hsv[1], False
    return (brightest or (128.0, 128.0, 128.0)), max(brightest_hsv[0], 0.0), brightest_hsv[1], False


def _frost_rank(v: float, s: float, is_skin: bool) -> int:
    """Desirability of a candidate frost colour, high = better:
      3 = a genuine non-skin colour   2 = a skin tone
      1 = white / grey (bright but colourless)   0 = too dark to trust.
    Used to decide whether the whole-poster fallback beats the local region. A
    colour is only "too dark to trust" when it is dark AND lacks chroma (near-black
    noise); a dark but vivid hue (deep navy/teal) stays a usable colour."""
    if v < _FROST_CONFIDENT_V and v * s < 0.05:
        return 0
    if s < _FROST_CHROMATIC_S:
        return 1
    return 2 if is_skin else 3


def dominant_frost_rgb(
    region: Image.Image, fallback: Image.Image | None = None
) -> tuple[float, float, float]:
    """Representative *actual* colour of a poster region for frosted tinting.

    Returns the region's most prominent real colour (see _dominant_cluster). When
    that colour is not the best kind available — it's too dark to carry a hue,
    washed out to white/grey, or merely a skin tone — and a broader ``fallback``
    region (typically the whole poster) offers a strictly better one (by
    _frost_rank: real colour > skin > white/grey > dark), that is borrowed
    instead. This keeps the frost matching a real poster colour, avoiding white
    and faces whenever the art offers something better, and only ever returns a
    colour genuinely present in the poster.
    """
    rgb, v, s, skin = _dominant_cluster(region)
    if rgb is None:
        rgb, v, s, skin = (128.0, 128.0, 128.0), 0.5, 0.0, False
    if fallback is not None:
        local_rank = _frost_rank(v, s, skin)
        if local_rank < 3:
            fb_rgb, fb_v, fb_s, fb_skin = _dominant_cluster(fallback)
            if fb_rgb is not None and _frost_rank(fb_v, fb_s, fb_skin) > local_rank:
                return fb_rgb
    return rgb


# The notch is drawn at 3x on every render, but its font, its shape and its
# label depend only on sizes and text — never on the poster underneath — so
# those parts are kept and only the frosted body (which is the poster, blurred)
# is redone.  The label layers are RGBA at 3x, 0.3-0.7 MB each, hence the
# small cap: enough for the award, status and trending labels a catalog
# actually repeats.
@lru_cache(maxsize=16)
def _notch_font(size_ss: int):
    try:
        return ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), size_ss)
    except IOError:
        return ImageFont.load_default()


@lru_cache(maxsize=32)
def _notch_shape(bw: int, bh: int, r_ss: int, frost_opacity: float) -> tuple[Image.Image, Image.Image]:
    """(shape mask, frost alpha): square top, rounded bottom corners."""
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (bw - 1, bh - 1)], radius=r_ss, fill=255,
        corners=(False, False, True, True)
    )
    rr_f = np.array(mask, dtype=np.float32) / 255
    return mask, Image.fromarray((rr_f * frost_opacity * 255).astype(np.uint8))


@lru_cache(maxsize=32)
def _notch_shape_1x(w: int, h: int, radius: int, frost_opacity: float) -> tuple[Image.Image, Image.Image]:
    """The frosted notch's (shape mask, frost alpha) at 1x.  PIL's rounded
    rectangle has no anti-aliasing, so the shape is drawn at 3x and box-reduced
    once; after that it is a cache hit, like the rest of the shape."""
    mask3, alpha3 = _notch_shape(w * 3, h * 3, radius * 3, frost_opacity)
    return mask3.reduce(3), alpha3.reduce(3)


@lru_cache(maxsize=64)
def _notch_label_layer_1x(label: str, size_ss: int, ss: int, w: int, h: int,
                          ink: tuple[int, int, int, int]) -> Image.Image:
    """The frosted notch's label at 1x, anti-aliased by FreeType itself.

    Positioned where the 3x layout puts it — the centre from _text_center at
    3x, divided down — and drawn from that baseline, rather than re-centred with
    1x metrics: those round to whole pixels (int(ascent * 0.22) above all) and
    sat the label a pixel high."""
    font3 = _notch_font(size_ss)
    tx, ty = _text_center(ImageDraw.Draw(Image.new("L", (1, 1))), label, font3, w * ss / 2, h * ss / 2)
    baseline = (ty + font3.getmetrics()[0]) / ss
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((tx / ss, baseline), label, font=_notch_font(size_ss // ss),
                               fill=ink, anchor="ls")
    return layer


def draw_award_badge(
    image: Image.Image,
    label: str,
    sash_type: str = "win",        # kept for colour wiring — may be used by future styles
    size_ratio_w: float = 1.0,     # horizontal scale multiplier
    size_ratio_h: float = 1.0,     # vertical scale multiplier
    notch_style: str = "frosted",     # "silver" | "gold" | "frosted"
    notch_inset: float = 0.004,        # top-edge offset as fraction of poster height (± small)
    notch_pad_ratio: float = 1.0,     # vertical padding scale; <1 tightens top/bottom space
    font_size_ratio: float = 0.43,    # font size as fraction of badge height
    frost_opacity: float = 0.75,      # frosted overlay opacity (0.0–1.0)
    frost_saturation: float = 1.2,    # frosted colour-cast strength (0 = grey)
    frost_reference: bool = False,    # match the poster colour instead of the pastel tint
    tint_rgb: tuple[float, float, float] | None = None,  # whole-poster colour (from un-graded art)
    star: bool | None = None,         # override ★ decision (resolved on canonical label)
    text_color: tuple[int, int, int] | None = None,  # override default white text
) -> Image.Image:
    """
    Centred notch badge that emerges from the top edge of the poster.
    Always horizontally centred; notch_inset nudges it up/down so users
    can control whether the top border is hidden or visible in their client.

    Vertical space is controlled by two independent knobs: size_ratio_h sets the
    nominal height (and with it the font scale), while notch_pad_ratio trims the
    empty space above and below the text without touching the font or the badge
    width.  Reach for notch_pad_ratio when the label looks lost in the notch.
    Trimming stops once the label's ink reaches the edges, and scales the corner
    radius and border with it; at font sizes that already fill the notch there is
    no empty space to reclaim and the control has no effect.

    Three styles:
      silver  — dark gradient body with silver trim, white text
      gold    — dark gradient body with gold trim, white text
      frosted — highly opaque blurred poster pixels, dark text

    sash_type colour wiring is retained for future use.
    Uses Cairo (sub-pixel AA, gradient) with PIL fallback. 3× LANCZOS downscale,
    except frosted, which is drawn at 1× (see that branch).
    """
    width, height = image.size

    SS = 3  # render at 3× then LANCZOS-downscale for crisp text and edges

    # ── Colour wiring (kept for potential future use by styles) ───────────────
    if sash_type == "win":
        border_rgb = (212, 175, 55)
    elif sash_type == "prestige":
        border_rgb = (190, 140, 255)
    elif sash_type == "cast":
        border_rgb = (102, 187, 106)
    elif sash_type == "info":
        border_rgb = (100, 220, 210)
    elif sash_type == "alert":
        border_rgb = (240, 100, 100)
    elif sash_type == "trending":
        border_rgb = (160, 220, 255)
    elif sash_type == "watchlist":
        border_rgb = (255, 190, 90)
    else:  # "nom"
        border_rgb = (192, 192, 200)

    # Style-specific trim colours (override sash colour for silver/gold)
    _SILVER = (192, 192, 200)
    _GOLD   = (212, 175, 55)
    trim_rgb = _SILVER if notch_style == "silver" else (_GOLD if notch_style == "gold" else border_rgb)

    # Winner marker: for awards whose win/nom labels are identical (Best Picture,
    # Golden Globe) the trim colour can't disambiguate them in notch mode (the
    # user's notch_style fixes it), so prefix a ★ for the winner — consistent
    # with the star in score/compact modes.  The badge width auto-expands to fit.
    # Double space after the star so it sits clearly left of the text rather than
    # crowding the first letter.  `star` (when provided) is resolved upstream on
    # the canonical English label so translated labels still get their marker.
    if star if star is not None else (sash_type == "win" and label in _STAR_WIN_AWARDS):
        label = f"★  {label}"

    # ── Dimensions ───────────────────────────────────────────────────────────
    # base_h is the nominal height size_ratio_h asks for.  It drives the font
    # size and the horizontal padding; notch_pad_ratio then scales only the
    # *drawn* height around that already-sized text.  Keeping the two separate
    # is what lets padding tighten without shrinking the label or narrowing the
    # badge — changing size_ratio_h alone moves both, which is rarely wanted.
    base_h = int(height * 0.075 * size_ratio_h)

    # ── Font: fixed size so every label renders at the same scale ────────────
    font_size_ss = int(base_h * font_size_ratio) * SS
    font = _notch_font(font_size_ss)

    # Measure rendered text at SS resolution — the ink extents drive both the
    # badge width and the vertical padding floor below.
    _tmp_d  = ImageDraw.Draw(Image.new("L", (1, 1)))
    _tbbox  = _tmp_d.textbbox((0, 0), label, font=font)
    text_w_ss = int(_tbbox[2] - _tbbox[0])

    # Vertical padding.  Floored so an aggressive notch_pad_ratio crops the empty
    # space but never the glyphs.  _text_center places the line box at
    # bh/2 - (ascent+descent)/2 - descent + int(ascent*0.22), so ink spans
    # bh/2 + _k + bbox[1] .. bh/2 + _k + bbox[3]; solving both ends for [0, bh]
    # gives the smallest height that still fits.  Measured against a reference
    # string of the tallest and deepest glyphs rather than the label itself, so
    # every award trims to the same height (cf. _REF in ratings.py) while
    # accented capitals still clear the border.
    _PAD_REF = "ÅÄÖÜÀÁÉÓÊÎÑÇgjpqy0★"
    _ref_bbox = _tmp_d.textbbox((0, 0), _PAD_REF, font=font)
    try:
        _ascent, _descent = font.getmetrics()
    except AttributeError:
        _ascent, _descent = 0, 0  # matches _text_center's own fallback
    _k = -(_ascent + _descent) / 2 - _descent + int(_ascent * 0.22)
    _ink_h_ss = max(-2 * (_k + _ref_bbox[1]), 2 * (_k + _ref_bbox[3]))
    _min_badge_h = math.ceil(_ink_h_ss * 1.05 / SS)  # 5% keeps ink off the border
    # The floor may only ever tighten the notch, never grow it past the height
    # the size and font ratios already asked for.  Without this clamp a
    # font_size_ratio above ~0.78 would raise badge_h even at the 1.0 default,
    # re-rendering saved URLs that predate this control.
    _min_badge_h = min(_min_badge_h, base_h)
    badge_h = max(_min_badge_h, int(base_h * notch_pad_ratio))
    bh      = badge_h * SS  # SS-space height (independent of width)

    # Badge width: minimum is size_ratio_w-scaled default; expands to fit text
    # with horizontal padding of ~45% of badge_h (22.5% each side).
    # Derived from base_h, not badge_h, so tightening the vertical padding
    # leaves the badge exactly as wide as it was.
    _h_pad    = int(base_h * 0.70)
    min_badge_w = int(width * 0.28 * size_ratio_w)
    max_badge_w = int(width * 0.70)
    badge_w   = max(min_badge_w, min(max_badge_w, text_w_ss // SS + _h_pad))

    # Corner radius and border scale with the drawn height, not base_h: a radius
    # derived from the untrimmed height would exceed half of a tightened badge
    # and distort the rounded rectangle.  Tightening therefore also thins the
    # border slightly, which keeps the notch in proportion.
    radius   = int(badge_h * 0.32)
    border_w = max(1, int(badge_h * 0.055))
    bw    = badge_w * SS
    r_ss  = radius   * SS
    bw_ss = border_w * SS

    # ── Position: always centred horizontally, inset controls top-edge offset ─
    bx = (width - badge_w) // 2
    by_composite = max(-badge_h, int(height * notch_inset))

    # Text is geometrically centred; client-specific placement is handled by inset.
    text_cy_ss = bh / 2

    if notch_style == "frosted":
        # ── Frosted: blurred poster crop tinted toward the region's dominant colour ──
        # Crop from the actual composite position so the blur matches what's visible
        crop_y = max(0, by_composite)
        region = image.crop((bx, crop_y, bx + badge_w, crop_y + badge_h))
        blur_r = max(4, int(badge_h * 0.35))
        # Drawn at 1x.  The 3x pass bought nothing here: the body is a blurred
        # crop (upscaling it 3x and back is a costly identity), the frost is a
        # flat colour, the shape mask is anti-aliased once and cached, and the
        # label is anti-aliased by FreeType.  ~1.4 ms instead of ~7.8 ms.
        blurred = region.filter(ImageFilter.GaussianBlur(radius=blur_r)).convert("RGBA")

        # Dominant colour of the actual poster region (a real cluster, not a
        # muddy mean — see dominant_frost_rgb).  tint_rgb (when supplied) overrides
        # it so the notch can match the frosted rating bar.
        # Colour comes from tint_rgb (a whole-poster sample the caller takes from
        # the un-graded art); the blurred texture still comes from the image.
        if tint_rgb is not None:
            dr, dg, db = tint_rgb
        else:
            dr, dg, db = dominant_frost_rgb(image)

        # Boost toward a bright, saturated version of that colour so the tint
        # reads clearly: floor V so dark regions lift, scale S by frost_saturation,
        # then mix 60 % of that tint with 40 % white for the frosted feel (or, in
        # reference mode, hew to the poster's true colour).
        fr_r, fr_g, fr_b = _frosted_tint(dr, dg, db, frost_saturation, frost_reference)

        # Notch shape mask (square top, rounded bottom)
        mask, frost_alpha = _notch_shape_1x(badge_w, badge_h, radius, frost_opacity)

        # Lay blurred crop under the tinted frost layer (alpha ~210 = quite opaque)
        blurred.putalpha(mask)
        frost = Image.new("RGBA", (badge_w, badge_h), (fr_r, fr_g, fr_b, 0))
        frost.putalpha(frost_alpha)
        badge = Image.alpha_composite(blurred, frost)

        # Text: dark on a light panel, light on a dark one (a matched panel can be
        # either — every other frost is light by construction).
        # (text_color is deliberately not consulted here: this style has always
        # ignored it, and honouring it now would restyle existing posters.)
        badge = Image.alpha_composite(badge, _notch_label_layer_1x(
            label, font_size_ss, SS, badge_w, badge_h, (*_frost_ink(fr_r, fr_g, fr_b), 245)
        ))

        result = image.copy()
        result.alpha_composite(badge, (bx, by_composite))
        return result

    if notch_style == "black":
        # ── Pure black: dark near-opaque body, no border, silver/white text ──
        badge_ss = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        rr_mask_ss = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(rr_mask_ss).rounded_rectangle(
            [(0, 0), (bw - 1, bh - 1)], radius=r_ss, fill=255,
            corners=(False, False, True, True)
        )
        body = Image.new("RGBA", (bw, bh), (10, 10, 12, 230))
        body.putalpha(rr_mask_ss)
        badge_ss = body
        txt_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        td = ImageDraw.Draw(txt_layer)
        tx, ty = _text_center(td, label, font, bw / 2, text_cy_ss)
        _txt_rgb_black = text_color if text_color is not None else (210, 210, 218)
        td.text((tx, ty), label, font=font, fill=(*_txt_rgb_black, 245))
        badge_ss = Image.alpha_composite(badge_ss, txt_layer)
        badge_final = badge_ss.resize((badge_w, badge_h), Image.Resampling.LANCZOS)
        result = image.copy()
        result.alpha_composite(badge_final, (bx, by_composite))
        return result

    body_alpha   = 235
    border_alpha = 215

    # ── Badge body + border (dark gradient, silver or gold trim) ─────────────
    # Always notch shape: square top corners, rounded bottom corners.
    # Cairo path for sub-pixel AA and gradient fill; PIL fallback otherwise.
    badge: Image.Image | None = None

    if _HAS_CAIRO:
        try:
            surface = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, bw, bh)
            ctx     = _cairo.Context(surface)
            ctx.set_antialias(_cairo.ANTIALIAS_BEST)

            def _rrect_notch(x: float, y: float, w: float, h: float, r: float) -> None:
                """Notch shape: square top corners, rounded bottom corners only."""
                ctx.move_to(x, y)
                ctx.line_to(x + w, y)
                ctx.line_to(x + w, y + h - r)
                ctx.arc(x + w - r, y + h - r, r,  0,           math.pi / 2)
                ctx.line_to(x + r, y + h)
                ctx.arc(x + r,     y + h - r, r,  math.pi / 2, math.pi)
                ctx.line_to(x, y)
                ctx.close_path()

            ba    = body_alpha / 255
            inset = bw_ss / 2

            # Dark gradient body (8 → 24 → 8 brightness, slight blue tint)
            d_lo = 4  / 255
            d_hi = 14 / 255
            grad = _cairo.LinearGradient(0, 0, 0, bh)
            grad.add_color_stop_rgba(0.0, d_lo, d_lo, d_lo * 1.3, ba)
            grad.add_color_stop_rgba(0.5, d_hi, d_hi, d_hi * 1.3, ba)
            grad.add_color_stop_rgba(1.0, d_lo, d_lo, d_lo * 1.3, ba)
            ctx.set_source(grad)
            _rrect_notch(0, 0, bw, bh, r_ss)
            ctx.fill()
            # Open-top trim: sides continue from the poster edge and wrap
            # around the rounded bottom, but no horizontal line can peek out
            # when a client crops or rounds the poster top.
            tr_c, tg_c, tb_c = trim_rgb
            ctx.set_source_rgba(tr_c / 255, tg_c / 255, tb_c / 255, border_alpha / 255)
            ctx.set_line_width(bw_ss)
            trim_r = max(1.0, r_ss - inset)
            ctx.move_to(inset, 0)
            ctx.line_to(inset, bh - inset - trim_r)
            ctx.arc_negative(inset + trim_r, bh - inset - trim_r, trim_r, math.pi, math.pi / 2)
            ctx.line_to(bw - inset - trim_r, bh - inset)
            ctx.arc_negative(bw - inset - trim_r, bh - inset - trim_r, trim_r, math.pi / 2, 0)
            ctx.line_to(bw - inset, 0)
            ctx.stroke()

            surface.flush()
            stride = surface.get_stride()
            buf    = bytes(surface.get_data())
            arr = (
                np.frombuffer(buf, dtype=np.uint8)
                .reshape((bh, stride))[:, : bw * 4]
                .reshape((bh, bw, 4))
                .copy()
            )
            # Cairo ARGB32 is premultiplied; un-premultiply to get straight RGBA.
            # Memory order per pixel: [B, G, R, A] (little-endian 32-bit word).
            a_f    = arr[:, :, 3].astype(np.float32)
            safe_a = np.where(a_f > 0, a_f, 1.0)
            r_s = np.clip(arr[:, :, 2].astype(np.float32) * 255.0 / safe_a, 0, 255).astype(np.uint8)
            g_s = np.clip(arr[:, :, 1].astype(np.float32) * 255.0 / safe_a, 0, 255).astype(np.uint8)
            b_s = np.clip(arr[:, :, 0].astype(np.float32) * 255.0 / safe_a, 0, 255).astype(np.uint8)
            rgba  = np.stack([r_s, g_s, b_s, arr[:, :, 3]], axis=2)
            badge = Image.fromarray(rgba)
        except Exception:
            badge = None

    if badge is None:
        # ── PIL fallback (always notch shape) ─────────────────────────────────
        t     = np.linspace(0, 1, bh, dtype=np.float32)
        b_arr = np.zeros((bh, bw, 4), dtype=np.uint8)
        darkness = (4 + 10 * np.sin(t * np.pi)).astype(np.uint8)
        b_arr[:, :, 0] = darkness[:, np.newaxis]
        b_arr[:, :, 1] = darkness[:, np.newaxis]
        b_arr[:, :, 2] = np.minimum(255, (darkness * 1.3).astype(np.uint8))[:, np.newaxis]
        b_arr[:, :, 3] = body_alpha
        body = Image.fromarray(b_arr)

        _nc = dict(corners=(False, False, True, True))
        body_mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(body_mask).rounded_rectangle(
            [(0, 0), (bw - 1, bh - 1)], radius=r_ss, fill=255, **_nc
        )
        body.putalpha(body_mask)
        border_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        border_draw = ImageDraw.Draw(border_layer)
        border_draw.rounded_rectangle(
            [(0, 0), (bw - 1, bh - 1)],
            radius=r_ss, outline=(*trim_rgb, border_alpha), width=bw_ss, **_nc,
        )
        # Remove only the horizontal top stroke, retaining both vertical sides.
        border_draw.rectangle(
            [(bw_ss, 0), (bw - bw_ss - 1, bw_ss)], fill=(0, 0, 0, 0)
        )
        badge = Image.alpha_composite(body, border_layer)

    # ── Text: white on dark body, with drop shadow ───────────────────────────
    txt_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt_layer)
    tx, ty = _text_center(td, label, font, bw // 2, text_cy_ss)
    _txt_rgb = text_color if text_color is not None else (255, 255, 255)
    td.text((tx + SS, ty + SS), label, font=font, fill=(0, 0, 0, 160))
    td.text((tx, ty),           label, font=font, fill=(*_txt_rgb, 235))
    badge = Image.alpha_composite(badge, txt_layer)

    # ── Downscale → composite ────────────────────────────────────────────────
    badge = badge.resize((badge_w, badge_h), Image.Resampling.LANCZOS)
    result = image.copy()
    result.alpha_composite(badge, (bx, by_composite))
    return result


def _frost_ink(r: float, g: float, b: float) -> tuple[int, int, int]:
    """Label colour for a frosted panel of this colour — dark on light, light on
    dark.

    Every frosted surface was light by construction until matching let one take a
    tinted vignette's own colour, which can be genuinely dark; its label has to
    follow or the badge stops reading.  The threshold sits well below any colour
    the other two modes produce (their luminance floor is 0.60), so this only ever
    changes what a matched panel does.
    """
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return (0, 0, 0) if lum >= 0.45 else (238, 238, 240)


def _frosted_tint(
    dr: float, dg: float, db: float, saturation: float = 1.2,
    reference: bool | str = False,
) -> tuple[int, int, int]:
    """Poster dominant RGB → the frosted tint shared by the bar, notch and sash so
    they stay consistent.

    Three modes:

    • Saturation (default) — a light "frosted glass" pastel. ``saturation`` scales
      the source S (1.2 = historical default; 0 = neutral grey). The colour is
      lifted to ~60 %+ Value and mixed 60/40 with white.

    • Match (``reference="match"``) — the frost is standing in for a colour that
      is already on the poster (a tinted vignette), so it must be *that colour*,
      lightness included.  Anything else is visibly a different colour sitting
      next to it: holding hue and chroma while lifting Value for legibility still
      reads as pale khaki beside dark olive, because lightness is most of what the
      eye calls "colour".  So the source is returned as it came, floored only
      short of black, and the caller flips its label to light ink — see
      _frost_ink.  ``saturation`` is ignored.

    • Reference (``reference=True``) — hew to the poster's *true* hue and
      saturation, dropping the pastel whitening so the frost closely matches the
      art. White is then added only as far as needed to lift the colour to a
      legibility floor, so the dark frosted text stays readable (an inherently
      dark hue like deep blue still gets enough white; an already-light gold keeps
      its full colour). ``saturation`` is ignored in this mode.

    Both key the colour's reliability on *chroma* (value × saturation), not Value,
    so a near-black noise pixel like (14, 4, 3) fades to neutral (no invented
    salmon) while a dark-but-vivid navy like (9, 35, 72) keeps its hue."""
    import colorsys
    h, s, v = colorsys.rgb_to_hsv(dr / 255, dg / 255, db / 255)
    # Confidence the source hue is real, from its chroma (v*s): ~0 below 0.05
    # (near-black/grey noise), full by 0.18 (a clear hue, however dark).
    conf = max(0.0, min(1.0, (v * s - 0.05) / 0.13))

    _PANEL_V = 0.85     # how light a dark-text frosted panel has to be
    _MATCH_MIN_V = 0.12  # ...and how dark a matched one is allowed to get

    if reference == "match":
        # The source colour, as it is.  Floored just clear of black so the panel
        # is still a surface rather than a hole; the caller's own gate is what
        # decides whether there was a colour worth matching in the first place.
        # No `conf` fade either — that judgement has already been made, and a
        # source close to neutral should come back close to neutral, not twice
        # faded.
        v_out = max(v, _MATCH_MIN_V)
        return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v_out))
    if reference:
        # True poster hue + saturation at a bright value, then just enough white
        # to reach a luminance floor for the dark text — vivid, not pastel.
        s_eff = min(1.0, s) * conf
        br, bg, bb = (c * 255 for c in colorsys.hsv_to_rgb(h, s_eff, max(v, _PANEL_V)))
        # White is added only as far as the dark text needs, so an already-light
        # colour keeps all of its own.
        lum = (0.299 * br + 0.587 * bg + 0.114 * bb) / 255
        _FLOOR = 0.60
        w = (_FLOOR - lum) / (1.0 - lum) if lum < _FLOOR else 0.0
        return (int(br * (1 - w) + 255 * w),
                int(bg * (1 - w) + 255 * w),
                int(bb * (1 - w) + 255 * w))

    s_eff = min(1.0, s * max(0.0, saturation)) * conf
    tr, tg, tb = colorsys.hsv_to_rgb(h, s_eff, v * 0.4 + 0.60)
    return (int(tr*255*0.6 + 255*0.4), int(tg*255*0.6 + 255*0.4), int(tb*255*0.6 + 255*0.4))


def _sash_skia(
    size: tuple[int, int],
    centre: tuple[float, float],
    origin: tuple[int, int],
    angle: float,
    strip: tuple[int, int],
    bands: tuple[float, float],
    colours: tuple[tuple[int, ...], ...],
    label: str,
    font_ss: Any,
    ss: int,
    text_rgb: tuple[int, int, int],
) -> Image.Image:
    """The sash's band and label, drawn at 1x by Skia onto a ``size`` canvas.

    Skia anti-aliases the turned rectangles and the turned text itself, so
    nothing is supersampled: the strip is drawn in its own coordinates under a
    rotate transform, straight onto a canvas covering only the corner.  Same
    geometry and colours as the PIL path; the label keeps the PIL path's
    centring (measured with the 3x font) and is drawn from that baseline."""
    w, h = size
    length, height = strip
    edge, margin = bands
    hi, lo, border, dark = colours
    info = _skia.ImageInfo.Make(w, h, _skia.kRGBA_8888_ColorType, _skia.kUnpremul_AlphaType)
    surface = _skia.Surface.MakeRaster(info)
    c = surface.getCanvas()
    c.clear(_skia.ColorTRANSPARENT)
    c.translate(centre[0] - origin[0], centre[1] - origin[1])
    c.rotate(angle)
    c.translate(-length / 2, -height / 2)

    col = lambda t: _skia.Color(t[0], t[1], t[2], t[3])
    # kSrc: each band replaces what is under it, as PIL's fills do — the dark
    # centre keeps its own 245 alpha rather than blending onto the gradient.
    p = _skia.Paint(AntiAlias=True, BlendMode=_skia.BlendMode.kSrc)
    p.setColor(col(border))
    c.drawRect(_skia.Rect(0, 0, length, height), p)
    # lo at the band's edges rising to hi at its middle — the PIL path's rows.
    p.setShader(_skia.GradientShader.MakeLinear(
        [_skia.Point(0, 0), _skia.Point(0, height)], [col(lo), col(hi), col(lo)], [0.0, 0.5, 1.0]))
    c.drawRect(_skia.Rect(0, edge, length, height - edge), p)
    c.drawRect(_skia.Rect(0, margin, length, height - margin),
               _skia.Paint(AntiAlias=True, BlendMode=_skia.BlendMode.kSrc, Color=col(dark)))

    tx, ty = _text_center(ImageDraw.Draw(Image.new("L", (1, 1))), label, font_ss,
                          length * ss / 2, height * ss / 2)
    x, baseline = tx / ss, (ty + font_ss.getmetrics()[0]) / ss
    font = _skia.Font(_SKIA_TYPEFACE, font_ss.size / ss)
    font.setSubpixel(True)
    font.setEdging(_skia.Font.Edging.kAntiAlias)
    tp = _skia.Paint(AntiAlias=True)
    tp.setColor(_skia.Color(0, 0, 0, 180))
    c.drawString(label, x + 2, baseline + 2, font, tp)
    tp.setColor(_skia.Color(*text_rgb, 225))
    c.drawString(label, x, baseline, font, tp)

    out = np.empty((h, w, 4), dtype=np.uint8)
    surface.readPixels(info, out)
    return Image.fromarray(out)             # (h, w, 4) uint8 → RGBA


def draw_award_sash(
    image: Image.Image,
    label: str,
    sash_type: str = "win",
    muted: bool = False,
    length_ratio: float = 1.15,
    height_ratio: float = 0.12,
    poster_color: tuple[float, float, float] | None = None,
    frost_saturation: float = 1.2,
    frost_reference: bool = False,
    star: bool = False,
    text_color: tuple[int, int, int] | None = None,
    side: str = "right",
) -> Image.Image:
    if star:
        label = f"★  {label}"
    width, height = image.size
    left = side == "left"

    # SS = the PIL fallback's supersample factor: it draws the band at SS× and
    # box-reduces it, which anti-aliases the edges and label (2× leaves visibly
    # stepped edges on the diagonal; 3× does not).  The Skia path draws at 1×
    # but keeps the SS-based sizes and centring, so both lay the sash out alike.
    SS          = 3
    sash_length = int(width * length_ratio)
    sash_height = int(width * height_ratio)

    sl, sh = sash_length * SS, sash_height * SS

    if poster_color is not None:
        # Poster-derived colour: tint the band edges / border from the art (same
        # logic the frosted notch uses).  The dark centre + light text are kept.
        _t = _frosted_tint(*poster_color, saturation=frost_saturation, reference=frost_reference)
        hi            = (*_t, 255)
        lo            = tuple(max(0, int(c * 0.6)) for c in _t) + (255,)
        border_colour = (*_t, 255)
    elif sash_type == "win":
        hi, lo        = (212, 175, 55, 255), (160, 130, 40, 255)
        border_colour = (212, 175, 55, 255)
    elif sash_type == "prestige":
        hi, lo        = (160, 100, 230, 255), (100, 55, 160, 255)
        border_colour = (190, 140, 255, 255)
    elif sash_type == "cast":
        hi, lo        = (46, 125, 50, 255), (27, 94, 32, 255)
        border_colour = (102, 187, 106, 255)
    elif sash_type == "info":
        hi, lo        = (60, 190, 180, 255), (30, 130, 120, 255)
        border_colour = (100, 220, 210, 255)
    elif sash_type == "alert":
        hi, lo        = (200, 55, 55, 255), (145, 25, 25, 255)
        border_colour = (240, 100, 100, 255)
    elif sash_type == "trending":
        hi, lo        = (90, 170, 255, 255), (50, 110, 190, 255)
        border_colour = (160, 220, 255, 255)
    elif sash_type == "watchlist":
        # Amber: warm like the gold win sash but clearly not it, and unlike
        # the red alert tier, so a queued title reads as "mine", not "urgent".
        hi, lo        = (235, 150, 40, 255), (170, 100, 20, 255)
        border_colour = (255, 190, 90, 255)
    else:  # "nom"
        hi, lo        = (180, 180, 190, 255), (110, 110, 120, 255)
        border_colour = (192, 192, 200, 255)

    margin = int(sh * 0.12)
    edge   = max(2 * SS, sh // 18)
    # The muted sash has always had a faintly blue-black centre.
    dark   = (8, 8, 14, 245) if muted else (8, 8, 8, 245)

    # Geometry.  The sash is a horizontal sl×sh strip turned 45° about its
    # centre — clockwise in the top-right corner (label reads downhill), counter-
    # clockwise in the top-left (reads uphill) — and hung so 68 % of its turned
    # bounding box overlaps the poster.  Rather than draw that strip and rotate
    # it (a ~1350² px bicubic resample of mostly empty canvas, which was most of
    # the render's time), the band goes straight onto a canvas covering only the
    # part of the poster it touches: Skia draws it there turned, at 1×; the PIL
    # fallback maps its edges there as polygons at SS× and rotates only the
    # small label layer.
    _a = math.radians(45)
    rw = math.ceil((sl + sh) * math.cos(_a))          # turned bounding box at SS
    sw = rw // SS
    # rw / SS (not the floored sw) on the left, so the two corners are exact
    # mirror images rather than a sub-pixel apart.
    x0 = (int(sw * 0.68) - rw / SS) if left else (width - int(sw * 0.68))
    y0 = -int(sw * 0.32)
    ct, st = math.cos(_a), math.sin(-_a if left else _a)

    def _to_poster(u: float, v: float) -> tuple[float, float]:
        """Strip coordinates at SS → poster coordinates at 1×."""
        du, dv = u - sl / 2, v - sh / 2
        return (x0 + (rw / 2 + du * ct - dv * st) / SS,
                y0 + (rw / 2 + du * st + dv * ct) / SS)

    corners = [_to_poster(u, v) for u, v in ((0, 0), (sl, 0), (sl, sh), (0, sh))]
    pad = 32   # beyond the poster edge, so the shadow blur has real band to spread
    ox = max(math.floor(min(p[0] for p in corners)), -pad)
    oy = max(math.floor(min(p[1] for p in corners)), -pad)
    ex = min(math.ceil(max(p[0] for p in corners)), width + pad)
    ey = min(math.ceil(max(p[1] for p in corners)), height + pad)

    def _q(u: float, v: float) -> tuple[float, float]:
        px, py = _to_poster(u, v)
        return ((px - ox) * SS, (py - oy) * SS)

    def _band(v0: float, v1: float) -> list[tuple[float, float]]:
        return [_q(0, v0), _q(sl, v0), _q(sl, v1), _q(0, v1)]

    base_size     = sash_height * 0.4
    adjusted_size = sash_height * 0.85 / (len(label) ** 0.35)
    font_size     = int(min(base_size, adjusted_size)) * SS

    font: Any
    try:
        font = ImageFont.truetype(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"), font_size)
    except IOError:
        font = ImageFont.load_default()

    _txt_rgb = text_color if text_color is not None else (225, 225, 225)
    if _HAS_SKIA:
        sash = _sash_skia(
            (ex - ox, ey - oy), _to_poster(sl / 2, sh / 2), (ox, oy), -45 if left else 45,
            (sash_length, sash_height), (edge / SS, margin / SS), (hi, lo, border_colour, dark),
            label, font, SS, _txt_rgb,
        )
    else:
        canvas = Image.new("RGBA", ((ex - ox) * SS, (ey - oy) * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        # Border, then the gradient rows between border and centre, then the dark
        # centre.  Each is a band nested inside the last, so the thin diagonal
        # gradient rows overlap rather than abut and can never leave a gap.
        d.polygon(_band(0, sh), fill=border_colour)
        half = sh // 2
        for y in range(edge + 1, margin):
            t = y / half
            d.polygon(_band(y, sh - y), fill=tuple(int(lo[i] * (1 - t) + hi[i] * t) for i in range(4)))
        d.polygon(_band(margin, sh - margin), fill=dark)

        # Label: drawn level on a layer just wide enough for it, centred on the band,
        # then that layer alone is turned and laid on the band.
        _probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        _bb    = _probe.textbbox((0, 0), label, font=font)
        lw     = max(1, int(_bb[2] - _bb[0] + 8 * SS))
        text_layer = Image.new("RGBA", (lw, sh), (0, 0, 0, 0))
        td         = ImageDraw.Draw(text_layer)

        tx, ty = _text_center(td, label, font, lw / 2, sh / 2)
        td.text((tx + 2 * SS, ty + 2 * SS), label, font=font, fill=(0, 0, 0, 180))
        td.text((tx, ty),                   label, font=font, fill=(*_txt_rgb, 225))

        text_layer = text_layer.rotate(45 if left else -45, expand=True, resample=Image.Resampling.BICUBIC)
        cx, cy = _q(sl / 2, sh / 2)
        lx, ly = round(cx - text_layer.width / 2), round(cy - text_layer.height / 2)
        sx0, sy0 = max(0, -lx), max(0, -ly)
        sx1 = min(text_layer.width, canvas.width - lx)
        sy1 = min(text_layer.height, canvas.height - ly)
        if sx1 > sx0 and sy1 > sy0:
            canvas.alpha_composite(text_layer, (lx + sx0, ly + sy0), (sx0, sy0, sx1, sy1))

        sash = canvas.reduce(SS)

    if muted:
        # Scale alpha to ~80% — sits level with the art rather than above it,
        # without making the text hard to read.
        r, g, b, a = sash.split()
        a = a.point(lambda v: int(v * 0.8))
        sash = Image.merge("RGBA", (r, g, b, a))

    shadow   = Image.new("RGBA", sash.size, (0, 0, 0, 0))
    sd       = ImageDraw.Draw(shadow)
    sd.bitmap((0, 0), sash.split()[3], fill=(0, 0, 0, 110))
    shadow   = shadow.filter(ImageFilter.GaussianBlur(10))

    result   = image.copy()
    # The shadow falls down and away from the corner.
    shadow_dx = -6 if left else 6
    result.paste(shadow, (ox + shadow_dx, oy + 6), shadow)
    result.paste(sash,   (ox,             oy),     sash)

    return result
