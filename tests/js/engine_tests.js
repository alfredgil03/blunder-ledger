// Known-position checks for the practice bot. Runs under node or JavaScriptCore.
// Globals provided by the bundle: Chess (chess.js) and __api (the engine).
var __results = { passed: 0, failed: 0, failures: [], timings: [] };

function check(name, cond, detail) {
  if (cond) { __results.passed++; }
  else { __results.failed++; __results.failures.push(name + (detail ? " :: " + detail : "")); }
}

// Deterministic noise so the bot's move choice is reproducible.
var __seed = 12345;
Math.random = function () { __seed = (__seed * 1664525 + 1013904223) % 4294967296; return __seed / 4294967296; };

function g(fen) { return new Chess(fen); }

// ---- findHangingPiece: capturable with no legal recapture ----
(function () {
  var hang = __api.findHangingPiece(g("4k3/8/8/3b4/8/8/8/3QK3 w - - 0 1"), "b");
  check("hanging: undefended bishop found", hang && hang.square === "d5" && hang.piece === "b", JSON.stringify(hang));
  var guarded = __api.findHangingPiece(g("4k3/8/4p3/3b4/8/8/8/3QK3 w - - 0 1"), "b");
  check("hanging: pawn-guarded bishop is not bait", guarded === null, JSON.stringify(guarded));
  var wrongTurn = __api.findHangingPiece(g("4k3/8/8/3b4/8/8/8/3QK3 b - - 0 1"), "b");
  check("hanging: wrong side to move returns null", wrongTurn === null);
})();

// ---- findFork ----
(function () {
  var fork = __api.findFork(g("r3r1k1/8/8/1N6/8/8/8/7K w - - 0 1"), "b");
  check("fork: knight to c7 forks two rooks", !!fork && fork.via === "Nc7", JSON.stringify(fork));
  var kingOnly = __api.findFork(g("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"), "b");
  check("fork: king plus one rook is not counted (two non-king targets required)", kingOnly === null, JSON.stringify(kingOnly));
  var none = __api.findFork(g("4k3/8/8/8/8/8/8/4K3 w - - 0 1"), "b");
  check("fork: bare kings return null", none === null);
})();

// ---- findPin ----
(function () {
  var pin = __api.findPin(g("4k3/4n3/8/8/8/8/8/R5K1 w - - 0 1"), "b");
  check("pin: Re1 pins the knight to the king", !!pin, JSON.stringify(pin));
  var already = __api.findPin(g("4k3/4n3/8/8/8/8/8/4R1K1 w - - 0 1"), "b");
  check("pin: an already-existing pin does not re-fire", already === null, JSON.stringify(already));
  var none = __api.findPin(g("4k3/8/8/8/8/8/8/R5K1 w - - 0 1"), "b");
  check("pin: nothing to pin returns null", none === null);
})();

// ---- findDiscoveredAttack ----
(function () {
  var disc = __api.findDiscoveredAttack(g("4q1k1/8/8/8/4N3/8/8/4R1K1 w - - 0 1"), "b");
  check("discovered: knight stepping off reveals rook on queen", !!disc, JSON.stringify(disc));
  var none = __api.findDiscoveredAttack(g("3q2k1/8/8/8/4N3/8/8/4R1K1 w - - 0 1"), "b");
  check("discovered: victim off the line returns null", none === null, JSON.stringify(none));
})();

// ---- bot move: legality, sanity, timing ----
function botMove(fen, color) {
  __api.state.game = g(fen);
  __api.state.botColor = color;
  __api.state.ratingChoice = "auto";
  var t0 = Date.now();
  var res = __api.computeBotMove();
  __results.timings.push(Date.now() - t0);
  return res;
}

(function () {
  var positions = [
    ["rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1", "b"],
    ["r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4", "w"],
    ["r2q1rk1/ppp2ppp/2n1bn2/3pp3/3P4/2N1PN2/PPP1BPPP/R2Q1RK1 w - - 0 9", "w"]
  ];
  positions.forEach(function (p) {
    var res = botMove(p[0], p[1]);
    var legal = !!res && !!g(p[0]).move(res.mv);
    check("bot: returns a legal move for " + p[0].split(" ")[0].slice(0, 12), legal, JSON.stringify(res && res.mv && res.mv.san));
  });
  var free = botMove("4k3/8/8/3q4/8/2N5/8/4K3 w - - 0 1", "w");
  check("bot: takes a free queen", free && free.mv.san === "Nxd5", free && free.mv.san);
  var guardedPawn = botMove("4k3/8/4p3/3p4/8/8/8/3RK3 w - - 0 1", "w");
  check("bot: does not throw a rook at a defended pawn", guardedPawn && guardedPawn.mv.san !== "Rxd5", guardedPawn && guardedPawn.mv.san);
  var mate = botMove("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1", "w");
  check("bot: plays the back-rank mate", mate && mate.mv.san === "Ra8#", mate && mate.mv.san);
})();

// ---- full game: no illegal moves, no exceptions ----
(function () {
  var game = new Chess();
  var plies = 0, ok = true, err = "";
  try {
    while (!game.game_over() && plies < 40) {
      var color = game.turn();
      __api.state.game = game; __api.state.botColor = color;
      var res = __api.computeBotMove();
      if (!res || !game.move(res.mv)) { ok = false; err = "no legal move at ply " + plies; break; }
      plies++;
    }
  } catch (e) { ok = false; err = String(e); }
  check("bot: 40-ply self-play stays legal", ok, err);
})();

// ---- timing budget (generous: catches a regression to multi-second moves) ----
(function () {
  var worst = Math.max.apply(null, __results.timings);
  check("bot: slowest single move under 5s", worst < 5000, worst + "ms");
})();

// ---- evaluation ----
(function () {
  var up = __api.evalPosition(g("4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1"), "w");
  var down = __api.evalPosition(g("4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1"), "b");
  check("eval: queen up is positive for its owner and mirrored for the other side", up > 800 && down < -800 && Math.abs(up + down) < 1, up + "/" + down);
})();

// ---- history summary ----
(function () {
  var docs = [
    { mode: "game", result: "win", byType: { fork: { offered: 2, taken: 1 } }, punished: { hung_material: 1 } },
    { mode: "game", result: "loss", byType: { fork: { offered: 2, taken: 2 }, pin: { offered: 1, taken: 0 } }, punished: {} },
    { mode: "review", attempted: 10, correct: 7 }
  ];
  var s = __api.summarizeHistory(docs);
  check("history: game count and record", s.games === 2 && s.record.w === 1 && s.record.l === 1, JSON.stringify(s.record));
  check("history: overall bait-taken rate is 3 of 5 = 60%", s.overall === 60 && s.totals.taken === 3 && s.totals.offered === 5, JSON.stringify(s.totals));
  check("history: per-pattern totals", s.byType.fork.offered === 4 && s.byType.fork.taken === 3 && s.byType.pin.offered === 1);
  check("history: punished count and review totals", s.punished === 1 && s.review.correct === 7 && s.review.attempted === 10 && s.reviewSessions === 1);
})();

var __out = JSON.stringify({ passed: __results.passed, failed: __results.failed, failures: __results.failures, timings_ms: __results.timings });
if (typeof console !== "undefined" && typeof process !== "undefined" && process.stdout) { console.log(__out); if (__results.failed) process.exit(1); }
__out;
