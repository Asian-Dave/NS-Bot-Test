"""Kekkai rune puzzle solver — Mastermind over six runes.

WHAT THE PUZZLE IS
------------------
Seen live in the TP Training mission "The Kekkai in the Forest". The screen says
it outright: *"Unseal the kekkai by clicking the runes in order"*.

  * a kekkai (barrier) diagram with N ordered slots — 3 in the mission observed
  * SIX rune buttons along the bottom: green spiral, red spiral, blue triangle,
    black lightning, yellow flame, white crescent
  * a history scroll on the right, one row per guess, each row showing TWO
    counters
  * a HUD reading "Seals: 0 / 2" — the mission needs two kekkai unsealed

Two counters per guess is the giveaway: this is Mastermind. The pair is
(correct rune in the correct place, correct rune in the wrong place).

WHERE THE ALGORITHM CAME FROM
-----------------------------
The reference bot (`ref/tp/cmmhero`) solves this exact puzzle — for the Jounin
and Sage exams, not for TP. Its dict is even called `jouninKekkai`. Its TP mode
(`FormDailyTP.cs`) is only a settings form and just fights N battles, so none of
its TP code helps; but its rune solver transfers directly.

Their implementation (`-/-.cs`, class `_2003`, and `FormMain.cs:11169`):

  * rune set is exactly `["Green","Red","Blue","Black","Yellow","White"]`
  * all candidate codes for lengths 1..6 are precomputed once in a static ctor
  * `solve(length, history)` filters the candidate list down to those that would
    have produced the SAME (correctPlace, wrongPlace) for every past guess, then
    returns a survivor
  * the scoring function is memoised on `"guess|candidate"`
  * the exam supports code lengths 2..5 (four coordinate tables, keyed 2..5)

So the strategy is pure consistency filtering: never guess something already
ruled out. That is what makes it correct; it cannot get stuck as long as the
feedback is read correctly.

WHAT WE DO DIFFERENTLY
----------------------
* **We pick the survivor that minimises the worst case**, not an arbitrary one.
  Their code returns `list[0]` after filtering. Consistency filtering alone
  already converges, but choosing the guess whose worst-case partition is
  smallest (a cheap minimax, Knuth's idea) converges in measurably fewer guesses
  — and guesses are the scarce resource here, since a wrong one costs a life in
  the sibling hand-seal minigame and presumably progress here too. The exhaustive
  self-test below reports the actual distribution rather than asserting a bound.
* **Repeats are allowed by default.** We do not know yet whether the game's codes
  can repeat a rune. Allowing them is the safe assumption: the candidate set is a
  superset, so consistency filtering still contains the true code. With 6 runes
  and length 3 that is 216 candidates instead of 120 — trivial either way. Set
  `allow_repeats=False` once it has been measured.

WHAT IS STILL UNVERIFIED — read before wiring this to clicks
------------------------------------------------------------
* The mapping from each rune button to its (x, y) needs measuring on our client;
  the reference bot's `jouninKekkai` coordinates are for its own geometry.
* How the two feedback counters are READ. The reference bot has dedicated
  routines (`JouninCorrectRuneCorrectPlace` / `...WrongPlace`) that scrape them
  off the screen. We need digit templates or a colour read for our own layout.
  Until that exists, this solver has no input and cannot run live.
* Whether a submitted guess needs a separate confirm click.
"""
import itertools
from collections import Counter

RUNES = ("Green", "Red", "Blue", "Black", "Yellow", "White")


def score(guess, secret):
    """Mastermind feedback: (correct rune correct place, correct rune wrong place).

    Exact positional hits are counted first and REMOVED from both multisets, then
    the wrong-place count is the multiset intersection of what remains. Counting
    the intersection without removing the exact hits first is the classic bug —
    it double-counts a rune that is both correctly placed and duplicated.
    """
    if len(guess) != len(secret):
        raise ValueError("guess and secret must be the same length")
    exact = sum(g == s for g, s in zip(guess, secret))
    grem = Counter(g for g, s in zip(guess, secret) if g != s)
    srem = Counter(s for g, s in zip(guess, secret) if g != s)
    loose = sum(min(n, srem[r]) for r, n in grem.items())
    return exact, loose


def candidates(length, runes=RUNES, allow_repeats=True):
    if allow_repeats:
        return [tuple(c) for c in itertools.product(runes, repeat=length)]
    return [tuple(c) for c in itertools.permutations(runes, length)]


def consistent(pool, history):
    """Candidates that would have produced every observed feedback.

    `history` is [(guess, correct_place, wrong_place), ...].
    """
    out = pool
    for guess, cp, wp in history:
        out = [c for c in out if score(guess, c) == (cp, wp)]
    return out


def next_guess(length, history, runes=RUNES, allow_repeats=True, minimax=True,
               minimax_cap=300):
    """The rune sequence to try next, or None if the history is contradictory.

    A None return is meaningful and must not be treated as "guess anything": it
    means no code is consistent with the feedback recorded so far, i.e. a counter
    was misread or a click did not register. Guessing on regardless would burn
    attempts against a corrupted model. Re-read the history instead.
    """
    pool = consistent(candidates(length, runes, allow_repeats), history)
    if not pool:
        return None
    if len(pool) <= 2 or not minimax or len(pool) > minimax_cap:
        # `minimax_cap` is a cost guard, not a quality choice. Minimax is O(n^2)
        # in the pool size: at length 4 with repeats the pool starts at 1,296,
        # which is ~1.7M scorings for a single guess. Above the cap we take the
        # first survivor - still always consistent, so still always correct - and
        # minimax takes over once consistency filtering has shrunk the pool.
        return pool[0]

    # Knuth-style minimax over the surviving pool: prefer the guess whose worst
    # feedback bucket is smallest, so the next filter removes the most.
    best, best_worst = None, None
    for g in pool:
        buckets = Counter(score(g, c) for c in pool)
        worst = max(buckets.values())
        if best_worst is None or worst < best_worst:
            best, best_worst = g, worst
    return best


def solve_interactive(length, ask, max_guesses=12, **kw):
    """Drive a full solve. `ask(guess) -> (correct_place, wrong_place)`.

    Returns (secret, guesses_used) on success, or (None, guesses_used).
    `ask` is where the live version plugs in: click the runes, submit, read the
    two counters. Keeping it a callback means the solver itself is testable with
    no game attached — which is how the self-test below runs.
    """
    history = []
    for n in range(1, max_guesses + 1):
        g = next_guess(length, history, **kw)
        if g is None:
            return None, n - 1
        cp, wp = ask(g)
        if cp == length:
            return g, n
        history.append((g, cp, wp))
    return None, max_guesses


# ---------------------------------------------------------------------------
def _self_test():
    """Every possible secret must be found, for lengths 2..4.

    Exhaustive except length-4-with-repeats, which is stride-sampled 1/7 (1,296
    secrets is slow and adds nothing over a sample). The output labels which rows
    are sampled.

    Asserting a guess-count BOUND would be inventing a number, so this reports
    the measured distribution and only asserts what must be true — that every
    secret is solved, and that a contradictory history returns None.
    """
    ok = True
    for length in (2, 3, 4):
        for repeats in (True, False):
            secrets = candidates(length, allow_repeats=repeats)
            # Length 4 with repeats is 1,296 secrets; exhaustive there is slow
            # and adds nothing over a stride sample, so say so rather than
            # quietly shrinking the claim.
            sampled = len(secrets) > 400
            if sampled:
                secrets = secrets[::7]
            worst, total = 0, 0
            for secret in secrets:
                found, used = solve_interactive(
                    length, lambda g, s=secret: score(g, s),
                    allow_repeats=repeats)
                if found != secret:
                    print(f"  FAIL len={length} repeats={repeats} "
                          f"secret={secret} got={found}")
                    ok = False
                worst = max(worst, used)
                total += used
            print(f"  len={length} repeats={repeats:<5} "
                  f"{len(secrets):>4} secrets{' (sampled 1/7)' if sampled else '':<15} "
                  f"avg={total/len(secrets):.2f} worst={worst} guesses")

    # A contradictory history must return None, not a wild guess.
    bogus = [(("Green",) * 3, 3, 0), (("Red",) * 3, 3, 0)]
    if next_guess(3, bogus) is not None:
        print("  FAIL contradictory history did not return None")
        ok = False
    else:
        print("  contradictory history -> None (correct)")

    # score() sanity, including the duplicate case the naive version gets wrong
    checks = [
        (("Green", "Red", "Blue"), ("Green", "Red", "Blue"), (3, 0)),
        (("Green", "Red", "Blue"), ("Blue", "Green", "Red"), (0, 3)),
        (("Green", "Green", "Red"), ("Green", "Red", "Green"), (1, 2)),
        (("Green", "Green", "Green"), ("Green", "Red", "Blue"), (1, 0)),
    ]
    for g, s, want in checks:
        got = score(g, s)
        if got != want:
            print(f"  FAIL score({g},{s}) = {got}, want {want}")
            ok = False
    print(f"  score() edge cases: {'all correct' if ok else 'FAILURES'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    print("kekkai solver self-test (no game required)")
    sys.exit(_self_test())
