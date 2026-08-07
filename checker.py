#!/usr/bin/env python3
"""
cloudflare.pay username availability helper
=============================================

A personal-use tool for picking your own Cloudflare Wallet handle
(https://cloudflare.pay). Main steps, run as subcommands:

    python checker.py generate     # build usernames.txt with candidate ideas
    python checker.py check        # check them concurrently against the real site

How the "check" step works
---------------------------
cloudflare.pay's signup page has a single text box (#tag) that checks name
availability in real time as you type. Inspecting that page's own network
traffic shows exactly what it does: it calls `GET /api/check?tag=<name>`
and shows you whatever that call returns, with NOTHING submitted or
reserved until you separately click "Reserve". This script calls that
same endpoint directly over plain HTTP -- the exact same request the
site's own front-end makes, just without rendering a page around it. No
browser, no Selenium, no Playwright, no Chrome process of any kind. It
never calls anything but this one read-only check endpoint, so no
account/reservation is ever made by running it.

Resume support
---------------
Every username that gets a definitive answer (available / taken / reserved /
invalid) is appended to checked.log immediately. If you stop the script
(Ctrl+C, crash, closed terminal) and run it again, already-checked names are
skipped automatically -- so it's always safe to resume.

Output
------
available.txt gets a new entry the moment a name is confirmed available:

    erased
    AVAILABLE

One pair of lines per hit, blank line between entries, easy to select/copy.
"""

import argparse
import os
import queue
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures, FIRST_COMPLETED
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
USERNAMES_FILE = BASE_DIR / "usernames.txt"
AVAILABLE_FILE = BASE_DIR / "available.txt"
CHECKED_LOG = BASE_DIR / "checked.log"
ERRORS_LOG = BASE_DIR / "errors.log"
WORDLIST_POOL_FILE = BASE_DIR / "wordlist_pool.txt"

# The exact endpoint cloudflare.pay's own signup page calls when you type
# into its "Choose your wallet name" box (found by inspecting that page's
# network traffic). This is the whole "browser automation" replaced by a
# single HTTP GET -- same request, same response, no page around it.
CHECK_API_URL = "https://cloudflare.pay/api/check"
REQUEST_TIMEOUT = 10  # seconds, per HTTP attempt
USER_AGENT = "cloudflare-pay-username-checker/2.0 (personal use, HTTP-only)"

# Number of concurrent workers checking usernames. Each worker is just a
# requests.Session (a small connection-pool object, not a process), so
# this is cheap to run high -- no browsers involved. Still worth being a
# reasonable neighbor to the site, which --delay-min/--delay-max below
# also help with.
WORKERS = 15

# How many usernames are ever "in flight" (queued but not yet checked) at
# once, across all workers combined. Usernames are fed into the queue in
# batches as workers consume them, instead of loading the whole run's
# worth into memory/the queue up front.
BATCH_QUEUE_MAXSIZE = 200

# On Ctrl+C, how long to wait for in-flight requests to wrap up and save
# their result before giving up on them and exiting anyway. Keeps
# shutdown bounded even if a request is genuinely stuck (e.g. a hung
# connection) -- an abandoned in-flight check is always safe: nothing is
# written to checked.log until a result actually comes back, so it just
# gets retried on the next run.
SHUTDOWN_GRACE_SECONDS = 3

# Site's own validation rules, confirmed by probing the live /api/check
# endpoint by hand before writing this script:
#   - lowercase letters, digits, hyphens only
#   - 3 to 32 characters
VALID_TAG_RE = re.compile(r"^[a-z0-9-]{3,32}$")

# Names that are near-certain to come back RESERVED_TAG on this kind of
# platform -- skipping them locally saves a round trip. The site is still
# the source of truth; anything not in this tiny list just gets checked
# for real.
LIKELY_RESERVED = {
    "admin", "administrator", "test", "cloudflare", "support", "help",
    "login", "root", "system", "staff", "moderator", "mod", "official",
    "api", "www", "null", "undefined", "security", "billing",
}


# ---------------------------------------------------------------------------
# Colored terminal output
# ---------------------------------------------------------------------------

class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _enable_windows_ansi():
    """Make ANSI colors work in classic cmd.exe / older PowerShell hosts."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


# ---------------------------------------------------------------------------
# Step 1: username generation
# ---------------------------------------------------------------------------

# The user-specified seed list, in priority order (deduplicated). This is
# the core "style anchor" -- every other word below was picked to match its
# feel: real dictionary words with an erased/classified/dark aesthetic.
SEED_WORDS = [
    "erased", "buried", "banished", "hollow", "faded", "forgotten",
    "classified", "redacted", "archived", "void", "silent", "shadow",
    "abandoned", "vanished", "expunged", "discarded", "lost", "hidden",
    "unknown", "nameless", "faceless", "echo", "remnant", "residue",
    "dormant", "coldcase", "blackfile", "dossier", "offrecord", "sealed",
    "restricted",
]

# Same theme, expanded with real synonyms grouped by sub-theme -- no
# combinatorics, no numbers, no letter/number tags. Every entry is a plain
# dictionary word (or a recognizable compound in the same style as
# "coldcase" / "blackfile" / "offrecord" above).
ERASURE_WORDS = [
    "deleted", "wiped", "purged", "scrubbed", "voided", "nullified",
    "effaced", "removed", "obliterated", "blotted",
]
SECRECY_WORDS = [
    "confidential", "encrypted", "concealed", "cloaked", "veiled",
    "masked", "ciphered", "undisclosed", "unlisted", "unindexed",
    "untraceable", "clandestine", "covert", "cryptic",
]
ABSENCE_WORDS = [
    "missing", "gone", "absent", "departed", "disappeared", "unseen",
    "unfound", "untraced",
]
SHADOW_WORDS = [
    "shadowed", "dusk", "twilight", "murk", "gloom", "obscured",
    "eclipsed", "blackout", "dimmed",
]
SILENCE_WORDS = [
    "mute", "hushed", "quiet", "still", "stagnant", "isolated",
    "solitary", "empty", "null", "blank", "barren",
]
MEMORY_WORDS = [
    "unremembered", "unnamed", "anonymous", "obscure", "unheard",
    "unmarked", "unclaimed", "untitled",
]
RUIN_WORDS = [
    "decayed", "ruined", "worn", "weathered", "crumbled", "derelict",
    "forsaken", "forlorn", "neglected", "desolate",
]
RECORD_WORDS = [
    "blacksite", "deadfile", "coldfile", "casefile", "blacklist",
    "watchlist",
]

# Further real-word themes that share the same dark / rare / mysterious
# feel and read naturally as Discord/Roblox/LARP-style one-word handles.
DECAY_WORDS = [
    "rot", "wither", "withered", "corpse", "carcass", "grave", "tomb",
    "crypt", "ashes", "rust", "mold", "blight", "plague", "pale",
]
MYSTIC_WORDS = [
    "specter", "phantom", "ghost", "ghostly", "banshee", "revenant",
    "shade", "wisp", "spirit", "haunt", "haunted", "cursed", "doomed",
    "forsworn", "forbidden",
]
COLD_WORDS = [
    "frost", "frozen", "frostbite", "glacier", "arctic", "permafrost",
    "blizzard", "numb", "chill", "chilled", "icy",
]
NIGHT_WORDS = [
    "nocturne", "nightfall", "midnight", "moonless", "starless", "umbra",
    "penumbra", "obsidian", "onyx", "ebony", "jet", "sable", "raven",
    "crow", "nightshade",
]
FRACTURE_WORDS = [
    "broken", "shattered", "cracked", "fractured", "splintered",
    "severed", "torn", "ruptured", "fissured",
]
DIGITAL_WORDS = [
    "static", "glitch", "glitched", "corrupted", "corrupt", "offline",
    "disconnected", "unplugged", "error", "fatal", "crash", "crashed",
    "nil",
]
INVESTIGATION_WORDS = [
    "informant", "witness", "testimony", "subpoena", "warrant",
    "indictment", "verdict", "evidence", "exhibit", "archive", "ledger",
    "logbook", "transcript", "deposition", "affidavit",
]
EXILE_WORDS = [
    "operative", "asset", "mole", "defector", "exile", "exiled",
    "fugitive", "renegade", "rogue", "outlaw", "outcast", "pariah",
    "deserter", "traitor", "saboteur", "insurgent", "dissident",
]
OCCULT_WORDS = [
    "ritual", "sacrifice", "offering", "relic", "artifact", "totem",
    "omen", "curse", "hex", "sigil", "ward", "ember", "cinder",
]
EMOTION_WORDS = [
    "dread", "despair", "anguish", "sorrow", "grief", "melancholy",
    "malaise", "numbness", "apathy", "emptiness", "hollowed",
]
OBSOLETE_WORDS = [
    "bygone", "obsolete", "deprecated", "outdated", "expired", "lapsed",
    "defunct", "extinct",
]
WEATHER_WORDS = [
    "fog", "mist", "haze", "smog", "overcast", "storm", "tempest",
    "thunder",
]
MINERAL_WORDS = [
    "charcoal", "slate", "graphite", "granite", "basalt", "flint",
    "quartz", "marble",
]
# Other natural inflections of roots already on the list (e.g. "erase" /
# "erasing" alongside "erased") -- still single real dictionary words in
# the same theme, just a different grammatical form.
INFLECTION_WORDS = [
    "erase", "erasing", "eraser", "bury", "burying", "forget",
    "forgetting", "hide", "hiding", "vanish", "vanishing", "abandon",
    "abandoning", "silence", "silencing", "shadowy", "fade", "fading",
    "lose", "losing", "conceal", "concealing", "classify", "redact",
    "redacting", "archiving", "discard", "discarding", "expunge",
    "expunging", "obscuring", "decay", "decaying", "ruin", "ruining",
    "withering", "corrupting", "break", "breaking", "shatter",
    "shattering", "sever", "severing", "haunting", "curse", "cursing",
    "doom", "dooming", "freeze", "freezing", "numbing", "chilling",
    "crack", "cracking", "fracture", "fracturing", "splinter",
    "splintering", "tear", "tearing", "rupture", "rupturing",
    "blighting", "plaguing", "mask", "masking", "veil", "veiling",
    "cloak", "cloaking", "encrypt", "encrypting", "seal", "sealing",
    "restrict", "restricting", "isolate", "isolating", "neglect",
    "neglecting", "forsake", "forsaking", "weathering", "crumble",
    "crumbling", "depart", "departing", "disappear", "disappearing",
    "miss", "missed", "effacing", "obliterate", "obliterating", "scrub",
    "scrubbing", "purge", "purging", "blot", "blotting", "wipe",
    "wiping", "delete", "deleting", "remove", "removing", "mute",
    "muting", "hush", "hushing", "empty", "emptying", "trace", "tracing",
]


def _valid(tag: str) -> bool:
    return bool(VALID_TAG_RE.match(tag)) and tag not in LIKELY_RESERVED


def _load_wordlist_pool() -> list[str]:
    """The big fill-in tier: real English words pulled from a public word
    list (dwyl/english-words), pre-filtered (offline, once) to drop the
    10,000 most common English words (so what's left leans rare) and any
    profanity/slurs (matched against the LDNOOBW block list). Beyond
    length and rarity this tier is NOT filtered for "dark/classified"
    meaning -- it exists purely to reach a larger count once the
    hand-curated, on-theme tiers are exhausted. Expect mixed vibe fit.
    """
    if not WORDLIST_POOL_FILE.exists():
        return []
    return [w.strip() for w in WORDLIST_POOL_FILE.read_text(encoding="utf-8").splitlines() if w.strip()]


def generate_usernames(count: int | None = None, seed: int = 42) -> list[str]:
    """Build the one-word-only candidate list, in priority order:
    1) the exact seed words, as given
    2) thematically-matched real-word expansions, grouped by sub-theme
       (hand-curated -- every entry genuinely fits the dark/classified/
       rare aesthetic)
    3) a large pool of rare real English words pulled from a public word
       list, used only to fill in beyond the curated tiers when a bigger
       `count` is requested -- length- and rarity-filtered, not meaning-
       filtered, so vibe fit is inconsistent in this tier
    No numbers, tags, or generated combinations at any tier -- every
    entry is a real dictionary word (or a compound in the same style as
    the seed list).
    """
    rng = random.Random(seed)
    seen: set[str] = set()
    ordered: list[str] = []

    def add(tag: str):
        if _valid(tag) and tag not in seen:
            seen.add(tag)
            ordered.append(tag)

    # Priority 1: the exact words given, in the order given.
    for word in SEED_WORDS:
        add(word)

    # Priority 2: real-word expansions, grouped by theme (order within
    # each group lightly shuffled so it doesn't read as alphabetical/rote).
    expansion_groups = [
        ERASURE_WORDS, SECRECY_WORDS, ABSENCE_WORDS, SHADOW_WORDS,
        SILENCE_WORDS, MEMORY_WORDS, RUIN_WORDS, RECORD_WORDS,
        DECAY_WORDS, MYSTIC_WORDS, COLD_WORDS, NIGHT_WORDS,
        FRACTURE_WORDS, DIGITAL_WORDS, INVESTIGATION_WORDS, EXILE_WORDS,
        OCCULT_WORDS, EMOTION_WORDS, OBSOLETE_WORDS, WEATHER_WORDS,
        MINERAL_WORDS, INFLECTION_WORDS,
    ]
    for group in expansion_groups:
        group = list(group)
        rng.shuffle(group)
        for word in group:
            add(word)

    # Priority 3: fill in from the public-wordlist pool, only if more are
    # still needed to reach `count`.
    if count and len(ordered) < count:
        for word in _load_wordlist_pool():
            add(word)
            if len(ordered) >= count:
                break

    return ordered[:count] if count else ordered


def cmd_generate(args):
    usernames = generate_usernames(args.count, seed=args.seed)
    USERNAMES_FILE.write_text("\n".join(usernames) + "\n", encoding="utf-8")
    print(f"{C.GREEN}Generated {len(usernames)} candidate usernames -> "
          f"{USERNAMES_FILE.name}{C.END}")


# ---------------------------------------------------------------------------
# Step 2: availability checking (direct HTTP to the site's own API -- no
# browser, no Selenium, no Playwright, no Chrome process)
# ---------------------------------------------------------------------------

def _load_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _append_line(path: Path, text: str):
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()


def _result_label(result):
    """Map a _check_one() result to the same colored status text the
    original sequential version printed -- unchanged wording/colors."""
    if result is None:
        return f"{C.YELLOW}ERROR (will retry next run){C.END}", None
    available, code = result
    if available:
        return f"{C.GREEN}{C.BOLD}AVAILABLE{C.END}", True
    if code == "TAG_TAKEN":
        return f"{C.RED}taken{C.END}", False
    if code == "RESERVED_TAG":
        return f"{C.YELLOW}reserved{C.END}", False
    if code == "INVALID_TAG":
        return f"{C.GRAY}invalid{C.END}", False
    return f"{C.GRAY}unknown ({code}){C.END}", False


def _worker_loop(worker_id, work_queue, stop_event, io_lock, stats, total,
                  args, requests_module):
    """One worker's whole life: its own requests.Session (just a small
    connection-pool object -- no process, no browser) pulling usernames
    off the shared queue until it sees its stop sentinel or a stop is
    requested. The session is opened once and reused for every check this
    worker does.
    """
    session = requests_module.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        while not stop_event.is_set():
            username = work_queue.get()
            if username is None:  # sentinel: no more work is coming
                work_queue.task_done()
                break

            try:
                result = _check_one(session, username, retries=3)
            finally:
                work_queue.task_done()

            label, available = _result_label(result)

            # Everything that touches shared state (files, the progress
            # counter, stdout) happens under one lock, so
            # available.txt/checked.log can't get corrupted or interleaved
            # and the counter stays accurate no matter how many workers
            # finish at once.
            with io_lock:
                stats["n"] += 1
                n = stats["n"]
                print(f"{C.GRAY}Checking {n}/{total}: {username}{C.END} {label}")
                if result is None:
                    _append_line(ERRORS_LOG, f"{username}\n")
                else:
                    if available:
                        _append_line(AVAILABLE_FILE, f"{username}\nAVAILABLE\n\n")
                        stats["found"] += 1
                    _append_line(CHECKED_LOG, f"{username}\n")

            time.sleep(random.uniform(args.delay_min, args.delay_max))
    except Exception as e:
        with io_lock:
            print(f"{C.YELLOW}Worker {worker_id} stopped early ({e}). "
                  f"Its remaining items will be picked up by other workers "
                  f"or the next run.{C.END}")
    finally:
        session.close()


def _feed_queue(items, work_queue, num_workers, stop_event):
    """Runs in its own thread. Puts usernames onto the bounded work_queue
    one at a time -- since the queue has a max size, this naturally feeds
    work in batches instead of loading the whole run's remaining list into
    the queue (and therefore memory) all at once. Once done (or stopped
    early), pushes one None sentinel per worker so every worker has a
    reliable way to know there's nothing left and exit its loop.
    """
    for item in items:
        if stop_event.is_set():
            break
        work_queue.put(item)  # blocks here when the queue is full -- that's the batching
    for _ in range(num_workers):
        work_queue.put(None)


def cmd_check(args):
    try:
        import requests
    except ImportError:
        print(f"{C.RED}The 'requests' package isn't installed.{C.END}\n"
              f"Run:\n  pip install -r requirements.txt")
        sys.exit(1)

    if not USERNAMES_FILE.exists():
        print(f"{C.RED}usernames.txt not found. Run "
              f"'python checker.py generate' first.{C.END}")
        sys.exit(1)

    all_usernames = _dedup_preserve_order(_load_list(USERNAMES_FILE))
    all_usernames = [u for u in all_usernames if _valid(u)]
    checked = set(_load_list(CHECKED_LOG))
    remaining = [u for u in all_usernames if u not in checked]

    total = len(all_usernames)
    # NOTE: intentionally total - len(remaining), not len(checked). If
    # usernames.txt was ever regenerated with different settings,
    # checked.log can contain names that aren't in the current list at
    # all -- len(checked) would overcount and make "already" bigger than
    # "total", producing nonsense like "Checking 5001/5000".
    already = total - len(remaining)

    if args.limit:
        remaining = remaining[: args.limit]

    print(f"{C.CYAN}{total} usernames total, {already} already checked "
          f"(resuming), {len(remaining)} to check this run.{C.END}\n")

    if not remaining:
        print(f"{C.GREEN}Nothing left to check.{C.END}")
        return

    num_workers = max(1, min(WORKERS, len(remaining)))
    io_lock = threading.Lock()
    stop_event = threading.Event()
    stats = {"n": already, "found": 0}

    # A shared, thread-safe, size-bounded queue is what guarantees no two
    # workers ever check the same username (Queue.get() hands each item to
    # exactly one caller) while also keeping only a small batch of
    # usernames "in flight" at once. A separate feeder thread trickles the
    # rest in as workers drain it, rather than loading everyone's worth of
    # remaining usernames into the queue up front.
    work_queue = queue.Queue(maxsize=min(BATCH_QUEUE_MAXSIZE, max(50, num_workers * 10)))
    feeder = threading.Thread(
        target=_feed_queue, args=(remaining, work_queue, num_workers, stop_event),
        daemon=True)
    feeder.start()

    print(f"{C.CYAN}Starting {num_workers} worker(s)...{C.END}\n")

    executor = ThreadPoolExecutor(max_workers=num_workers)
    futures = [
        executor.submit(_worker_loop, i + 1, work_queue, stop_event, io_lock,
                         stats, total, args, requests)
        for i in range(num_workers)
    ]

    interrupted = False
    try:
        # Poll with a short timeout instead of an unbounded wait. This is
        # what actually makes Ctrl+C work: a plain, timeout-less wait here
        # (e.g. the old `for f in as_completed(futures)`) blocks the main
        # thread in a C-level wait that Python/Windows won't interrupt
        # until a future completes on its own -- Ctrl+C can sit ignored
        # for a long time, or indefinitely. Waking up every 0.5s gives the
        # interpreter a chance to actually raise KeyboardInterrupt promptly.
        pending = set(futures)
        while pending:
            done, pending = wait_futures(pending, timeout=0.5,
                                          return_when=FIRST_COMPLETED)
            for f in done:
                f.result()  # re-raise anything unexpected from a worker
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n{C.CYAN}Stopping... saving progress...{C.END}")
        sys.stdout.flush()
        # Immediately stop the feeder from handing out more work, and tell
        # every worker to stop after whatever check it's currently doing.
        stop_event.set()

        done, pending = wait_futures(futures, timeout=SHUTDOWN_GRACE_SECONDS)
        if pending:
            # Something is still stuck past the grace period (a hung
            # connection, most likely). Don't wait on it forever -- just
            # exit. Every worker already closes its own session in a
            # finally block on the way out; anything still mid-request
            # simply never got a result, so it was never written to
            # checked.log and will be retried automatically next run.
            executor.shutdown(wait=False)
            print(f"{C.CYAN}Stopped (a couple of in-flight checks were "
                  f"abandoned safely). Progress saved -- rerun to "
                  f"resume.{C.END}")
            sys.stdout.flush()
            os._exit(0)

    executor.shutdown(wait=False)

    if interrupted:
        print(f"{C.CYAN}Stopped. Progress saved -- rerun to resume.{C.END}")
    else:
        print(f"\n{C.CYAN}Done this run. {stats['found']} new available "
              f"username(s) found -> {AVAILABLE_FILE.name}{C.END}")


def _check_one(session, username, retries=3):
    """Call cloudflare.pay's own availability-check endpoint directly --
    the exact request its front-end makes when you type into the "Choose
    your wallet name" box, confirmed by inspecting that page's network
    traffic. Nothing else: one GET, one JSON response, no page, no click,
    no reservation. Both HTTP 200 (available/taken) and HTTP 400
    (reserved/invalid) responses carry the JSON body we care about, so the
    status code itself is never inspected here -- only the parsed fields,
    same as before.
    Returns (available: bool, code: str|None) or None on repeated failure.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(CHECK_API_URL, params={"tag": username},
                                timeout=REQUEST_TIMEOUT)
            data = resp.json()
            return bool(data.get("available")), data.get("code")
        except Exception:
            if attempt == retries:
                return None
            time.sleep(1.5 * attempt)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _enable_windows_ansi()
    parser = argparse.ArgumentParser(
        description="cloudflare.pay username availability helper")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate usernames.txt")
    gen.add_argument("--count", type=int, default=5000,
                      help="target number of candidates (default 5000; curated "
                           "on-theme words first, filled out from wordlist_pool.txt)")
    gen.add_argument("--seed", type=int, default=42,
                      help="random seed, for reproducible lists")
    gen.set_defaults(func=cmd_generate)

    chk = sub.add_parser("check", help="check usernames.txt against the site")
    chk.add_argument("--limit", type=int, default=None,
                      help="only check the next N unchecked usernames")
    chk.add_argument("--delay-min", type=float, default=0.5,
                      help="minimum seconds between checks (default 0.5)")
    chk.add_argument("--delay-max", type=float, default=1.1,
                      help="maximum seconds between checks (default 1.1)")
    chk.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
