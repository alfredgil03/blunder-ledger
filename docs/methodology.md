# Methodology

This is how every number on the three pages gets made, so you can check it (or argue with it).

## How it's put together

There are three main scripts. `review.py` looks at one game, `bulk_review.py` looks at a batch of games and builds the Blunder Ledger, and `practice.py` builds the practice page from batches that were already saved, so it never runs the engine itself. `blunder.py` just runs them in order.

The tactic detector is in `motifs.py` and all three use it. The scoring formulas are copied into both `review.py` and `bulk_review.py` so each one reads on its own, and there's a test that makes sure the two copies still agree.

You can save any analysis as JSON and re-render the page later with `--from-json` without running Stockfish again.

## The engine

Every position goes through Stockfish (running locally) using python-chess.

| Script | Depth | Lines | Why |
|---|---|---|---|
| review.py | 16 | 2 | I need the second-best line to tell when there was only one good move |
| bulk_review.py | 12 | 1 | batch stats don't need that much detail, and it's about 2-3 seconds a game instead of a minute |

Scores are flipped to the point of view of whoever is moving, and a forced mate counts as +/- 100,000.

## Centipawn loss, win probability, and accuracy

Centipawn loss (CPL) is just how much worse a move made the position for the person who played it:

```
cpl = max(0, eval_before - eval_after)
```

Centipawns are hard to compare across a whole game, so I also turn each score into a win probability. This is the same curve Lichess uses, with the score capped at +/- 1000:

```
win% = 50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1)
```

Accuracy for a move comes from how much win probability you lost (also Lichess's formula, clamped between 0 and 100):

```
accuracy = 103.1668 * exp(-0.04354 * (win%_before - win%_after)) - 3.1668
```

Game accuracy is the average of your moves. One problem I ran into: if you're already below 10% or above 90% to win, the curve barely moves, so an actual blunder can still score close to 100. In that range I use whichever is lower, the formula or `100 - cpl / 5`.

The accuracy number isn't perfect. I compared it with chess.com on 21 games where they also gave an accuracy score. The two line up well (0.91 correlation), but mine comes out around 15 points higher on average. So it's good for comparing your games against each other, but it isn't the same scale as chess.com's. It also stays pretty high once a game is already lost, which is why the pages show a warning when accuracy looks high but there are a bunch of flagged moves.

## How moves get tagged (single-game review)

Every move gets compared to the engine's top two lines. The "gap" is how far ahead the best line is compared to the second best. If the gap is big, that basically means only one move worked.

| Tag | When |
|---|---|
| Book | still in a known opening (Lichess's opening list, matched by move order) |
| Brilliant | the engine's best move, it's a sacrifice, and the game wasn't already decided (within 5 pawns either way) |
| Great Find | the engine's best move with a gap of 150+, game not already decided, and not just grabbing a free piece |
| Best | the engine's best move otherwise |
| Excellent | CPL 1-15 |
| Good | CPL 16-50 |
| Inaccuracy | CPL 51-100 |
| Mistake | CPL 101-200, gap under 150 |
| Blunder | CPL over 200, gap under 150 |
| Miss | CPL over 100 with a gap of 150+ (there was one clear winning idea and you didn't find it) |

For Brilliant, a "sacrifice" means a piece (not a pawn or king) goes somewhere the opponent can take it with something strictly cheaper, after counting whatever it captured. Strictly cheaper matters here, since an even trade isn't a sacrifice.

The worst three moves are ranked by how much win probability they lost, not by CPL. A huge swing in a game that's already lost matters less than a smaller one that turned a close game into a loss. The best three come from Brilliant, Great Find, Excellent, and Best moves, ranked by the gap, meaning how much was riding on finding that move.

## The progress report

The batch report uses simpler cutoffs than the single game review: blunder is CPL 200+, mistake is 100-199, inaccuracy is 50-99.

Each blunder also gets a type based on the engine's best reply. If that reply is a capture, it's hung material. If the position after the move is already a forced mate, it's mate-bound (this is an approximation, not a checked mating line). Anything else is "other."

For game phases: the opening is moves 1-10, the endgame is when there are 6 or fewer knights, bishops, rooks, and queens left in total, and the middlegame is everything in between. Each phase bar is divided by how many moves you actually played in that phase so phases of different lengths are fair to compare.

Ratings come from every game on the account in one time class, because chess.com keeps a separate rating for rapid, blitz, etc.

## Tactics

Each flagged move gets checked two ways, only using board rules (no engine):
- what you missed, meaning the tactic the engine's move would have created
- what you allowed, meaning the tactic the opponent's best reply creates

The four tactics it looks for:
- pin: a new piece gets pinned to its king
- discovered check: check from a piece other than the one that moved
- discovered attack: an enemy piece gets a new attacker that isn't the piece that moved (a line opened up)
- fork: the piece that moved now attacks two or more pieces worth a knight or more, and it can't be taken by something cheaper. If the king can just take it, that counts as cheaper, so it's not a fork.

Pins and discovered checks are exact. Discovered attacks and forks are approximations since they don't check whether the target was defended or whether both threats can actually be answered. About two thirds of flagged moves don't match any of these, and the ledger shows that "other" group instead of hiding it.

## Error bars

The rates on the ledger (blunder rate and the tactic shares) have a 95% range, and I count it by game instead of by move. The reason is that one bad game usually has a bunch of blunders that aren't independent of each other, so if every move counted separately the ranges would look tighter than they really are. The "Is the change real?" note uses these ranges to compare the last 50 games to the 50 before.

## The pages

The review and ledger pages have the raw analysis built into them and calculate every number in the browser, so nothing is typed in by hand. When there are two batches, the Last 50 / Previous 50 / Both switch recalculates everything. The only written content is optional notes passed in with `--notes`.

## Practice bot (Blunder Bait)

The bot is a small engine written in JavaScript on top of chess.js instead of Stockfish. That's on purpose: its whole job is to sometimes pick a slightly worse move so it can set a trap, and I needed control over the search to do that.

- Strength: your latest rating picks one of five levels (under 350 up to 650+). Each level sets how many moves it considers, how deep it looks (2 half-moves, 3 at the top level), how random it is, and how much worse than the best move a trap move is allowed to be (90 centipawns down to 30). It also plays out captures at the end of a search so it doesn't misjudge trades.
- Traps: out of its near-best moves, it prefers one that leaves a hanging piece, a discovered attack, a fork, or a pin. Which one it tries first is random but weighted by how often you miss each kind.
- Punishing: if you allow one of those tactics and there's a sound way to take advantage, it does.
- Puzzles: each game from the last 7 days gives its single worst blunder or mistake, as long as material was within 5 points at the time. The 25 most costly are kept in the order they were played, and each one links to the game (and its full review, if one was made).
- History is only saved in the browser, so practice games never mess with your real stats.

## Limits

Chess.com doesn't publish its rules, so my tags are an approximation of theirs and won't always match. And openings are matched by move order, so if a game reaches a known position through a different order it won't get tagged as Book.
