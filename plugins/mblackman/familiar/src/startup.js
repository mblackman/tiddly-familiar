(function(){
"use strict";

exports.name = "tiddly-familiar";
exports.platforms = ["browser"];
exports.after = ["startup"];
exports.before = ["render"];
exports.synchronous = true;

exports.startup = function() {
  function dbg(msg) {
    $tw.wiki.addTiddler(new $tw.Tiddler({
      title: "$:/temp/familiar/debug",
      text: msg
    }));
    console.log("[familiar]", msg);
  }

  // One-time carry-over from the pre-rename "ai-gateway" plugin: copy any
  // saved config into the familiar/ namespace so an upgrade keeps the user's
  // GatewayURL/APIKey without re-entry. Only fills empty targets, so it never
  // clobbers new settings and is a no-op on fresh installs.
  function migrateLegacyConfig() {
    ["GatewayURL", "APIKey", "ChatNoteTemplate"].forEach(function(name) {
      var oldTitle = "$:/config/mblackman/ai-gateway/" + name;
      var newTitle = "$:/config/mblackman/familiar/" + name;
      var oldVal = $tw.wiki.getTiddlerText(oldTitle);
      if (oldVal && oldVal.trim() && !($tw.wiki.getTiddlerText(newTitle) || "").trim()) {
        $tw.wiki.addTiddler(new $tw.Tiddler({title: newTitle, text: oldVal}));
      }
    });
  }

  try {
    migrateLegacyConfig();

    var cfg = function(name) {
      return ($tw.wiki.getTiddlerText("$:/config/mblackman/familiar/" + name) || "").trim();
    };

    // Read per call so Control Panel settings changes apply without a reload.
    var baseURL = function() {
      return (cfg("GatewayURL") || "http://localhost:8787").replace(/\/+$/, "");
    };
    var apiKey = function() {
      return cfg("APIKey");
    };

    // Optional retrieval knobs from Control Panel -> Settings -> Familiar. Blank
    // (or unparseable) -> return null so the request omits the field and the
    // gateway falls back to its own env default. Read per call, like URL/key.
    function cfgInt(name) {
      var raw = cfg(name);
      if (!raw) return null;
      var n = parseInt(raw, 10);
      return (isFinite(n) && n > 0) ? n : null;
    }
    function cfgBool(name) {
      var raw = cfg(name).toLowerCase();
      if (raw === "yes" || raw === "true" || raw === "on" || raw === "1") return true;
      if (raw === "no" || raw === "false" || raw === "off" || raw === "0") return false;
      return null; // blank / unrecognised -> let the server decide
    }
    // /ask + /ask/stream overrides: top-k, query rewrite, embed-miss budget.
    function applyAskOverrides(body) {
      var topK = cfgInt("RagTopK");
      if (topK !== null) body.rag_top_k = topK;
      var rewrite = cfgBool("QueryRewrite");
      if (rewrite !== null) body.query_rewrite = rewrite;
      return applyMaxTiddlers(body);
    }
    // /search + /related share only the embed-miss budget knob.
    function applyMaxTiddlers(body) {
      var maxT = cfgInt("MaxTiddlers");
      if (maxT !== null) body.max_tiddlers = maxT;
      return body;
    }

    // This wiki sends its own note content with every request (the gateway's
    // client-supplied-content routes), so everything works against any
    // TiddlyWiki — the gateway needs no notebook config or wiki credentials.
    // Budgets mirrored from the gateway (MAX_CANDIDATE_TIDDLERS etc. in
    // main.py): enforce here by dropping overflow so requests never 422.
    //
    // The candidate set for a request is mostly bare {hash} refs (cheap), so
    // the whole synced corpus rides along and the gateway ranks over all of it,
    // not just a slice. Only full {title,text} content is tightly bounded.
    var LOCAL_MAX_CANDIDATES = 20000; // notes referenced per request (refs + fulls)
    var LOCAL_MAX_FULL = 500;         // full-content notes sent per request
    var LOCAL_MAX_TEXT = 50000;
    var LOCAL_MAX_TOTAL = 2000000;

    var CHAT_PREFIX = "$:/temp/familiar/chat/";
    // Saved chat turns live under this system prefix (keyed by note title) so
    // they persist via the wiki saver but stay out of Recent/search and the
    // gateway RAG push — only the conversation note itself reads as a note.
    var SAVED_CHAT_PREFIX = "$:/familiar/chat/";
    var SEARCH_RESULT_PREFIX = "$:/temp/familiar/search/result/";
    var CHAT_NOTE_STATE = "$:/state/familiar/chat-note";
    var NEW_TITLE_STATE = "$:/temp/volatile/familiar/new-chat-title";
    var NEW_NOTE_OPEN_STATE = "$:/state/familiar/new-note-open";
    var TITLE_TEMPLATE_DEFAULT = "AI Chat: {name}";
    var CHAT_TAG = "ai-chat";
    var CHAT_TURN_TAG = "ai-chat-turn";
    var MAX_TURNS = 10;
    var FLUSH_MS = 120; // throttle transcript re-renders while streaming

    // One-time migration (plugin <=0.12.0): saved chat turns used to be stored
    // as ordinary tiddlers ("<note>/turn/NNNNNN") that showed up in Recent and
    // got pushed to the gateway RAG index. Move any legacy turns under the
    // system SAVED_CHAT_PREFIX so they persist but stay out of the note feed.
    // (Titles already stray under "$:/" — temp/new turns — are left alone.)
    function migrateChatTurns() {
      $tw.wiki.filterTiddlers("[tag[" + CHAT_TURN_TAG + "]!prefix[$:/]]")
        .forEach(function(oldTitle) {
          var newTitle = SAVED_CHAT_PREFIX + oldTitle;
          if ($tw.wiki.tiddlerExists(newTitle)) return;
          var tid = $tw.wiki.getTiddler(oldTitle);
          $tw.wiki.addTiddler(new $tw.Tiddler(tid.fields, {title: newTitle}));
          $tw.wiki.deleteTiddler(oldTitle);
        });
    }
    migrateChatTurns();

    function headers() {
      var h = {"Content-Type": "application/json"};
      var key = apiKey();
      if (key) h["X-API-Key"] = key;
      return h;
    }

    function setState(title, text, type) {
      var fields = {title: title, text: text};
      if (type) fields.type = type;
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
    }

    function clearSearchResults() {
      $tw.wiki.filterTiddlers("[prefix[" + SEARCH_RESULT_PREFIX + "]]")
        .forEach(function(t) { $tw.wiki.deleteTiddler(t); });
    }

    function rejectWithDetail(r) {
      return r.json().catch(function() { return {}; }).then(function(errBody) {
        var err = new Error(errBody.detail || ("HTTP " + r.status));
        err.status = r.status;
        throw err;
      });
    }

    // --- chat transcript: one tiddler per turn ---
    // Unsaved chats live under the volatile CHAT_PREFIX ($:/temp/ is never
    // synced). "Save chat" creates a real note (tagged CHAT_TAG, shown in
    // Recent) and stores its turns under the system SAVED_CHAT_PREFIX keyed by
    // the note title. System turns persist via the wiki saver but are hidden
    // from Recent/search ([!is[system]]) and skipped by the gateway RAG push,
    // so only the conversation note surfaces as a note. CHAT_NOTE_STATE binds
    // the panel to the note.

    function boundChatNote() {
      var title = ($tw.wiki.getTiddlerText(CHAT_NOTE_STATE) || "").trim();
      return title && $tw.wiki.tiddlerExists(title) ? title : null;
    }

    // Where a note's turns live: system prefix for a saved note, the volatile
    // temp prefix for an unbound (unsaved) sidebar transcript.
    function turnPrefixFor(note) {
      return note ? SAVED_CHAT_PREFIX + note + "/turn/" : CHAT_PREFIX;
    }

    function chatPrefix() {
      return turnPrefixFor(boundChatNote());
    }

    function padTurn(n) {
      return ("000000" + n).slice(-6);
    }

    function chatTurnTitles(prefix) {
      prefix = prefix || chatPrefix();
      return $tw.wiki.filterTiddlers("[prefix[" + prefix + "]sort[title]]");
    }

    function chatHistoryFor(prefix) {
      return chatTurnTitles(prefix).map(function(title) {
        var t = $tw.wiki.getTiddler(title);
        return {
          role: t.fields.role === "assistant" ? "assistant" : "user",
          content: t.fields.text || ""
        };
      }).slice(-MAX_TURNS);
    }

    function chatHistory() {
      return chatHistoryFor(chatPrefix());
    }

    function turnFields(role, content, sources) {
      var fields = {text: content, role: role};
      if (role === "assistant") fields.type = "text/markdown";
      if (sources && sources.length) fields.sources = $tw.utils.stringifyList(sources);
      return fields;
    }

    function appendTurn(role, content, sources, note) {
      if (note === undefined) note = boundChatNote();
      var prefix = turnPrefixFor(note);
      var titles = chatTurnTitles(prefix);
      var last = titles.length
        ? parseInt(titles[titles.length - 1].slice(prefix.length), 10) : 0;
      var fields = turnFields(role, content, sources);
      fields.title = prefix + padTurn(last + 1);
      if (note) {
        fields.tags = [CHAT_TURN_TAG];
        $tw.wiki.addTiddler(new $tw.Tiddler(
          $tw.wiki.getCreationFields(), fields, $tw.wiki.getModificationFields()));
        // bump the note itself so the conversation surfaces in "Recent"
        $tw.wiki.addTiddler(new $tw.Tiddler(
          $tw.wiki.getTiddler(note), $tw.wiki.getModificationFields()));
        // a completed exchange updates the note's searchable digest
        if (role === "assistant") scheduleDigest(note);
        return;
      }
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
      titles = chatTurnTitles(prefix);
      while (titles.length > MAX_TURNS) {
        $tw.wiki.deleteTiddler(titles.shift());
      }
    }

    // --- conversation digest: distil a saved chat into its note body so past
    // conversations resurface in future retrieval. The turns stay hidden
    // system tiddlers; the note (already in Recent) gains a short plain-prose
    // summary as its searchable content. Best-effort — a failed digest never
    // touches the chat. The "digest-turns" field records how many turns the
    // current digest reflects, so we skip regenerating when nothing changed.
    var DIGEST_DEBOUNCE_MS = 4000;
    var digestTimers = {};

    function scheduleDigest(note) {
      if (!note) return;
      if (digestTimers[note]) clearTimeout(digestTimers[note]);
      digestTimers[note] = setTimeout(function() {
        delete digestTimers[note];
        refreshDigest(note);
      }, DIGEST_DEBOUNCE_MS);
    }

    function refreshDigest(note) {
      if (!$tw.wiki.tiddlerExists(note)) return;
      var titles = chatTurnTitles(turnPrefixFor(note));
      if (!titles.length) return;
      var noteTid = $tw.wiki.getTiddler(note);
      var mark = noteTid.fields["digest-turns"];
      // never clobber a body the user wrote themselves (non-empty, no digest
      // marker); once we've written one digest we own the body and refresh it.
      if (String(noteTid.fields.text || "").trim() && !mark) return;
      if (parseInt(mark || "0", 10) === titles.length) return;
      var transcript = titles.map(function(t) {
        var f = $tw.wiki.getTiddler(t).fields;
        return (f.role === "assistant" ? "Assistant: " : "User: ") +
          String(f.text || "").trim();
      }).join("\n\n").slice(0, LOCAL_MAX_TEXT);
      // Prefer the transcript-tuned "digest" command; an older gateway that
      // doesn't know it (400 Unknown command) falls back to plain "summarize".
      $tw.Familiar.generateText(note, "digest", transcript).catch(function(err) {
        if (/[Uu]nknown command/.test(err.message)) {
          return $tw.Familiar.generateText(note, "summarize", transcript);
        }
        throw err;
      }).then(function(data) {
        var summary = ((data && data.result) || "").trim();
        var cur = $tw.wiki.getTiddler(note);
        if (!summary || !cur) return;
        $tw.wiki.addTiddler(new $tw.Tiddler(cur, {
          text: summary, "digest-turns": String(titles.length)
        }, $tw.wiki.getModificationFields()));
        dbg("digest updated for " + JSON.stringify(note) + " (" + titles.length + " turns)");
      }).catch(function(err) {
        dbg("digest failed for " + JSON.stringify(note) + " (" + err.message + ") - non-fatal");
      });
    }

    // keep titles filter-safe: they are spliced into [prefix[...]] runs
    function sanitizeTitle(s) {
      return (s || "").replace(/[\[\]{}|<>]/g, " ").replace(/\s+/g, " ").trim();
    }

    function uniqueTitle(base) {
      var title = base, n = 1;
      while ($tw.wiki.tiddlerExists(title)) {
        n += 1;
        title = base + " (" + n + ")";
      }
      return title;
    }

    function chatNoteTitleFor(firstQuestion) {
      var slug = sanitizeTitle(firstQuestion).slice(0, 40).trim();
      if (!slug) slug = $tw.utils.formatDateString(new Date(), "YYYY-0MM-0DD 0hh:0mm");
      return uniqueTitle("AI Chat: " + slug);
    }

    // --- new-chat-note titles from a user-editable template ---
    // Template lives in $:/config/mblackman/familiar/ChatNoteTemplate;
    // tokens: {name} random adjective-noun pair, {date} YYYY-MM-DD, {time} HH:MM.

    var NAME_ADJECTIVES = ["amber", "bold", "brisk", "calm", "clever", "cosmic",
      "deft", "dusky", "eager", "gentle", "keen", "lively", "lucid", "mellow",
      "nimble", "quiet", "ruby", "silver", "sunny", "swift", "tidy", "vivid",
      "wandering", "witty"];
    var NAME_NOUNS = ["badger", "beacon", "comet", "falcon", "fern", "glacier",
      "harbor", "heron", "lantern", "maple", "meadow", "nebula", "otter",
      "pebble", "pine", "quill", "raven", "river", "sparrow", "thicket",
      "tide", "walnut", "willow", "wren"];

    function pick(list) {
      return list[Math.floor(Math.random() * list.length)];
    }

    function randomName() {
      return pick(NAME_ADJECTIVES) + "-" + pick(NAME_NOUNS);
    }

    function expandTitleTemplate() {
      var tpl = cfg("ChatNoteTemplate") || TITLE_TEMPLATE_DEFAULT;
      var now = new Date();
      var title = sanitizeTitle(tpl
        .replace(/\{name\}/g, randomName)
        .replace(/\{date\}/g, $tw.utils.formatDateString(now, "YYYY-0MM-0DD"))
        .replace(/\{time\}/g, $tw.utils.formatDateString(now, "0hh:0mm")));
      return title || ("AI Chat: " + randomName());
    }

    // --- local mode: note content travels in the request body, minimized
    // via the gateway's content-addressed note cache (/notes/check) ---

    // Pure-JS SHA-256: crypto.subtle is unavailable in insecure contexts
    // (plain-http wikis), and the hash must match app/note_cache.py's
    // canonical_hash byte-for-byte. Parity vectors (title, text, tags → hex)
    // shared with tests/test_note_cache.py:
    //   ("Zebra", "A zebra is a striped horse.", "")
    //     → 13c0a20134166a0c73da78321b8d6588431cb8cd11a5022f2276dd31b726985f
    //   ("Note", "", "")
    //     → d68e7ae2c3de5f151a1f463b4c33cd852817f0a8c3405cd02312fee9d280e94a
    //   ("Ünïcødé ✨", "tëxt 🦓", "TagA [[Tag B]]")
    //     → 9ca48d25d446f1e5724955761c10b8cd75d3351c96908032adfcae5286baca6b
    var SHA256_K = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ];

    function sha256Hex(str) {
      var bytes;
      if (typeof TextEncoder !== "undefined") {
        bytes = new TextEncoder().encode(str);
      } else {
        var utf8 = unescape(encodeURIComponent(str));
        bytes = new Uint8Array(utf8.length);
        for (var bi = 0; bi < utf8.length; bi++) bytes[bi] = utf8.charCodeAt(bi);
      }
      var len = bytes.length;
      var padded = new Uint8Array((((len + 8) >> 6) << 6) + 64);
      padded.set(bytes);
      padded[len] = 0x80;
      var dv = new DataView(padded.buffer);
      var bitLen = len * 8;
      dv.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000));
      dv.setUint32(padded.length - 4, bitLen >>> 0);
      var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
               0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
      var w = new Array(64);
      for (var off = 0; off < padded.length; off += 64) {
        var t;
        for (t = 0; t < 16; t++) w[t] = dv.getUint32(off + t * 4);
        for (t = 16; t < 64; t++) {
          var x = w[t - 15], y = w[t - 2];
          var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
          var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
          w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
        }
        var a = H[0], b = H[1], c = H[2], d = H[3];
        var e = H[4], f = H[5], g = H[6], h = H[7];
        for (t = 0; t < 64; t++) {
          var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
          var ch = (e & f) ^ (~e & g);
          var t1 = (h + S1 + ch + SHA256_K[t] + w[t]) >>> 0;
          var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
          var maj = (a & b) ^ (a & c) ^ (b & c);
          var t2 = (S0 + maj) >>> 0;
          h = g; g = f; f = e; e = (d + t1) >>> 0;
          d = c; c = b; b = a; a = (t1 + t2) >>> 0;
        }
        H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
        H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
        H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
        H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
      }
      var hex = "";
      for (var hi = 0; hi < 8; hi++) {
        hex += ("00000000" + H[hi].toString(16)).slice(-8);
      }
      return hex;
    }

    function noteHash(title, text, tags) {
      return sha256Hex(title + "\u0000" + text + "\u0000" + tags);
    }

    // Read one note in the canonical form both sides hash. Only `tags` goes
    // in fields — it's the only field the gateway's keyword scoring reads.
    // Text is truncated at the same limit the server enforces, and hashed
    // post-truncation so both sides hash identical content.
    function readNote(title) {
      var t = $tw.wiki.getTiddler(title);
      if (!t) return null;
      var tags = t.fields.tags ? $tw.utils.stringifyList(t.fields.tags) : "";
      var text = String(t.fields.text || "").slice(0, LOCAL_MAX_TEXT);
      return {
        title: title,
        text: text,
        fields: tags ? {tags: tags} : {},
        hash: noteHash(title, text, tags),
        modified: modSig(t)
      };
    }

    // --- background sync ---
    // syncState is the incremental title→{hash, synced} map: filled by an
    // idle-time scan at startup and kept current by the wiki change event, so
    // asks never rehash the corpus. `synced` means the server has confirmed
    // it holds this exact content (via /notes/check or a /notes/sync push);
    // synced notes travel as bare {hash} refs.
    var syncState = {};
    var pendingPush = {};   // titles changed since the last flush
    var pushTimer = null;
    var persistTimer = null;
    var PUSH_DEBOUNCE_MS = 5000;
    var SCAN_SLICE = 25;    // titles hashed per idle slice
    var SYNC_BATCH_NOTES = 100;
    var SYNC_BATCH_CHARS = 500000;
    var SYNC_CHECK_BATCH = 1000; // hashes per /notes/check (server caps at 2000)
    var SYNC_RETRY_BASE_MS = 3000;
    var SYNC_RETRY_MAX = 5;      // attempts before giving up (edits re-arm later)
    var SYNC_STATUS = "$:/temp/familiar/sync-status";

    // --- cross-reload persistence of the sync map ---
    // syncState is rebuilt on every wiki load; without this the whole corpus is
    // re-hashed (slow pure-JS sha256) each time. We persist title -> {hash,
    // synced, modified} in localStorage and, at scan time, reuse the stored
    // hash for any note whose `modified` stamp is unchanged — so a reload only
    // hashes notes edited while the tab was closed. Trusting a persisted
    // `synced` flag is safe: if the server pruned it meanwhile, the ask path's
    // 409 resend heals it. Best-effort — localStorage may be absent (file://,
    // private mode) or full, in which case we silently fall back to a full scan.
    var SYNC_PERSIST_KEY = "familiar:syncState:" +
      (typeof location !== "undefined" ? (location.host + location.pathname) : "");
    function modSig(tiddler) {
      return tiddler && tiddler.fields.modified
        ? $tw.utils.stringifyDate(tiddler.fields.modified) : "";
    }
    function loadPersistedSync() {
      try {
        var raw = window.localStorage.getItem(SYNC_PERSIST_KEY);
        var obj = raw ? JSON.parse(raw) : null;
        return (obj && typeof obj === "object") ? obj : {};
      } catch (e) { return {}; }
    }
    function persistSyncState() {
      try {
        var out = {};
        for (var title in syncState) {
          var e = syncState[title];
          out[title] = {hash: e.hash, synced: !!e.synced, modified: e.modified || ""};
        }
        window.localStorage.setItem(SYNC_PERSIST_KEY, JSON.stringify(out));
      } catch (e) { /* unavailable or over quota: persistence is optional */ }
    }
    function schedulePersist() {
      if (persistTimer) return;
      persistTimer = setTimeout(function() {
        persistTimer = null;
        persistSyncState();
      }, 2000);
    }

    // --- sync status readout ---
    // $:/temp/familiar/sync-status carries the coarse state in `text`
    // (scanning|syncing|ready|degraded) plus count fields the settings panel
    // renders so a user can see coverage and spot gaps. `server` is the
    // gateway's own note count (via /notes/stats), left blank until fetched.
    var serverCount = null; // last known server-held note count; null = unknown
    function setSyncState(state) {
      var total = 0, synced = 0;
      for (var t in syncState) { total++; if (syncState[t].synced) synced++; }
      var fields = {
        title: SYNC_STATUS,
        text: state,
        total: String(total),
        synced: String(synced),
        unsynced: String(total - synced),
        pending: String(Object.keys(pendingPush).length),
        checked: $tw.utils.stringifyDate(new Date())
      };
      if (serverCount !== null) fields.server = String(serverCount);
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
    }

    // Ask the gateway how many notes it holds and fold the number into the
    // status tiddler. Best-effort: a failure leaves the last known count.
    function refreshServerCount() {
      return fetch(baseURL() + "/notes/stats", {headers: headers()})
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(d) {
          if (d && typeof d.count === "number") serverCount = d.count;
        })
        .catch(function() {});
    }

    // POST hashes to /notes/check in <=SYNC_CHECK_BATCH chunks — a wiki with
    // thousands of notes would 422 a single request (server cap 2000) and, with
    // no retry, wedge sync in "degraded" forever. Resolves to a {hash:true} set
    // of the hashes the server is missing.
    function checkHashes(hashes) {
      var missing = {};
      function step(i) {
        if (i >= hashes.length) return Promise.resolve(missing);
        return fetch(baseURL() + "/notes/check", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({hashes: hashes.slice(i, i + SYNC_CHECK_BATCH)})
        }).then(function(r) {
          if (!r.ok) throw new Error("notes/check " + r.status);
          return r.json();
        }).then(function(d) {
          (d.missing || []).forEach(function(h) { missing[h] = true; });
          return step(i + SYNC_CHECK_BATCH);
        });
      }
      return step(0);
    }

    // Upload full content for `titles` in budgeted sequential batches. A note
    // is only marked synced if its hash is still the one that was pushed —
    // an edit racing the upload stays pending for the next flush.
    function pushTitles(titles) {
      if (!titles.length) return Promise.resolve();
      var batch = [], chars = 0, i = 0;
      while (i < titles.length && batch.length < SYNC_BATCH_NOTES && chars < SYNC_BATCH_CHARS) {
        var n = readNote(titles[i]);
        i++;
        if (!n) continue;
        batch.push(n);
        chars += n.text.length;
      }
      var rest = titles.slice(i);
      if (!batch.length) return pushTitles(rest);
      return fetch(baseURL() + "/notes/sync", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({tiddlers: batch.map(function(n) {
          return {title: n.title, text: n.text, fields: n.fields};
        })})
      }).then(function(r) {
        if (!r.ok) throw new Error("notes/sync " + r.status);
        batch.forEach(function(n) {
          var cur = syncState[n.title];
          if (cur && cur.hash === n.hash) {
            cur.synced = true;
            delete pendingPush[n.title];
          }
        });
        schedulePersist();
        return pushTitles(rest);
      });
    }

    function armPushFlush() {
      if (pushTimer) clearTimeout(pushTimer);
      pushTimer = setTimeout(function() {
        pushTimer = null;
        flushPending(0);
      }, PUSH_DEBOUNCE_MS);
    }

    // Flush queued edits with bounded backoff. A transient failure used to be
    // logged and dropped until the next edit re-armed the timer; now it retries
    // so a brief gateway blip self-heals. pushTitles clears each note from
    // pendingPush as it confirms, so a retry only re-sends what's still pending.
    function flushPending(attempt) {
      var titles = Object.keys(pendingPush);
      if (!titles.length) return;
      pushTitles(titles).then(function() {
        setSyncState($tw.wiki.getTiddlerText(SYNC_STATUS) || "ready");
      }).catch(function(err) {
        if (attempt + 1 >= SYNC_RETRY_MAX) {
          dbg("background push failed after retries (" + err.message +
              ") - will retry on next edit; asks still work");
          return;
        }
        var delay = SYNC_RETRY_BASE_MS * Math.pow(2, attempt);
        dbg("background push failed (" + err.message + ") - retrying in " + delay + "ms");
        setTimeout(function() { flushPending(attempt + 1); }, delay);
      });
    }

    // Keep the map current: rehash changed notes (one note per event — cheap)
    // and queue them for a debounced background push. Deletes just leave the
    // map; the server's TTL prunes unreferenced content. System titles are
    // skipped — $:/state and $:/temp churn constantly during streaming; the
    // rare filter that selects system tiddlers is covered by on-demand
    // hashing in localPayload.
    $tw.wiki.addEventListener("change", function(changes) {
      var dirty = false;
      for (var title in changes) {
        if (title.indexOf("$:/") === 0) continue;
        if (changes[title].deleted) {
          if (syncState[title]) dirty = true; // drop it from the persisted map too
          delete syncState[title];
          delete pendingPush[title];
          continue;
        }
        var n = readNote(title);
        if (!n) continue;
        syncState[title] = {hash: n.hash, synced: false, modified: n.modified};
        pendingPush[title] = true;
        dirty = true;
      }
      if (dirty) { schedulePersist(); armPushFlush(); }
    });

    // Build the sync map at startup, in idle-time slices so nothing janks:
    // reuse persisted hashes for notes unchanged since last load, rehash the
    // rest, then warm the server (chunked /notes/check, upload the missing).
    // After this, steady-state asks are pure hash refs with no preflight and
    // no hashing.
    function startInitialScan() {
      setSyncState("scanning");
      var persisted = loadPersistedSync();
      var titles = $tw.wiki.filterTiddlers("[!is[system]]");
      var i = 0, reused = 0;
      function idle(fn) {
        if (typeof requestIdleCallback === "function") requestIdleCallback(fn);
        else setTimeout(fn, 50);
      }
      function slice() {
        var end = Math.min(i + SCAN_SLICE, titles.length);
        for (; i < end; i++) {
          var title = titles[i];
          if (syncState[title]) continue; // change handler may have beaten us
          var t = $tw.wiki.getTiddler(title);
          if (!t) continue;
          var prev = persisted[title], sig = modSig(t);
          if (prev && prev.hash && sig && prev.modified === sig) {
            // Unchanged since last session: reuse the stored hash and the
            // server-confirmed flag, skipping the (slow) rehash. A stale
            // `synced` (server pruned it) self-heals via the ask 409 path.
            syncState[title] = {hash: prev.hash, synced: !!prev.synced, modified: sig};
            reused++;
          } else {
            var n = readNote(title);
            if (n) syncState[title] = {hash: n.hash, synced: false, modified: n.modified};
          }
        }
        if (i < titles.length) return idle(slice);
        dbg("scan done - " + titles.length + " note(s), " + reused + " reused from cache");
        warmServerWithRetry(0);
      }
      idle(slice);
    }

    // Warm the server, retrying transient failures with bounded backoff so a
    // gateway that's briefly down at load doesn't strand sync in "degraded"
    // until a manual reload. Success flips status to "ready" inside warmServer.
    function warmServerWithRetry(attempt) {
      warmServer().catch(function(err) {
        setSyncState("degraded");
        if (attempt + 1 >= SYNC_RETRY_MAX) {
          dbg("sync warm failed after retries (" + err.message +
              ") - asks degrade to full sends");
          return;
        }
        var delay = SYNC_RETRY_BASE_MS * Math.pow(2, attempt);
        dbg("sync warm failed (" + err.message + ") - retrying in " + delay + "ms");
        setTimeout(function() { warmServerWithRetry(attempt + 1); }, delay);
      });
    }

    function warmServer() {
      var titles = Object.keys(syncState);
      var toCheck = titles.filter(function(t) { return !syncState[t].synced; });
      return checkHashes(toCheck.map(function(t) { return syncState[t].hash; }))
        .then(function(missing) {
          var toPush = [];
          toCheck.forEach(function(t) {
            if (missing[syncState[t].hash]) toPush.push(t);
            else syncState[t].synced = true;
          });
          return pushTitles(toPush).then(function() {
            schedulePersist();
            return refreshServerCount().then(function() {
              setSyncState("ready");
              dbg("sync warm done - " + titles.length + " note(s) tracked, " +
                  toPush.length + " pushed");
            });
          });
        });
    }

    // Manual full sync (the settings "Sync now" button): pick up any notes the
    // idle scan / change events missed, re-check everything unsynced against
    // the gateway, push what it lacks, and refresh the server count. Runs the
    // same warmServer path as startup, so it's safe to invoke any time. A
    // second click while "syncing" is a no-op (guarded by the handler).
    function syncNow() {
      setSyncState("syncing");
      $tw.wiki.filterTiddlers("[!is[system]]").forEach(function(title) {
        if (syncState[title]) return; // already tracked
        var n = readNote(title);
        if (n) syncState[title] = {hash: n.hash, synced: false, modified: n.modified};
      });
      return warmServer().catch(function(err) {
        setSyncState("degraded");
        throw err;
      });
    }

    // Build the ask payload from the sync map: refs for server-confirmed
    // notes, one small /notes/check over only the unconfirmed ones (empty in
    // steady state → no preflight at all), full content for whatever the
    // server lacks. `resend(hashes)` upgrades refs to full content for the
    // 409 retry (a note evicted server-side after our last confirmation).
    function localPayload(filter) {
      var titles = $tw.wiki.filterTiddlers((filter || "").trim() || "[!is[system]]");
      var total = titles.length;
      var notes = [];
      titles.slice(0, LOCAL_MAX_CANDIDATES).forEach(function(title) {
        var e = syncState[title];
        if (!e) { // not scanned yet (mid-startup) or a system tiddler
          var n = readNote(title);
          if (!n) return;
          e = syncState[title] = {hash: n.hash, synced: false};
        }
        notes.push({title: title, hash: e.hash});
      });
      var mustSend = {};
      var payload = {tiddlers: [], total: total, capped: total > LOCAL_MAX_CANDIDATES};

      // Synced notes travel as bare {hash} refs (unbounded up to the candidate
      // cap). Only notes the server lacks are sent as full content, bounded by
      // count and total chars — overflow is dropped and self-heals once the
      // background sync caches it, so it comes back as a free ref next time.
      function build() {
        var out = [], budget = 0, fulls = 0;
        notes.forEach(function(n) {
          if (!mustSend[n.hash]) {
            out.push({hash: n.hash});
            return;
          }
          var full = readNote(n.title);
          if (!full) return;
          if (fulls >= LOCAL_MAX_FULL || budget + full.text.length > LOCAL_MAX_TOTAL) {
            payload.capped = true; // over budget: drop, self-heals once others cache
            return;
          }
          fulls++;
          budget += full.text.length;
          out.push({title: full.title, text: full.text, fields: full.fields});
        });
        payload.tiddlers = out;
        return out;
      }

      payload.resend = function(hashes) {
        (hashes || []).forEach(function(h) { mustSend[h] = true; });
        notes.forEach(function(n) { // server lost these: re-push in background
          var e = syncState[n.title];
          if (mustSend[n.hash] && e && e.hash === n.hash) e.synced = false;
        });
        return build();
      };

      var unsynced = notes.filter(function(n) {
        var e = syncState[n.title];
        return !(e && e.hash === n.hash && e.synced);
      });
      if (!unsynced.length) {
        build();
        return Promise.resolve(payload);
      }
      return checkHashes(unsynced.map(function(n) { return n.hash; })).then(function(missing) {
        var confirmed = false;
        unsynced.forEach(function(n) {
          var e = syncState[n.title];
          if (missing[n.hash]) mustSend[n.hash] = true;
          else if (e && e.hash === n.hash) { e.synced = true; confirmed = true; }
        });
        if (confirmed) schedulePersist();
        build();
        return payload;
      }).catch(function(err) {
        dbg("notes/check unavailable (" + err.message + ") - sending " +
            unsynced.length + " note(s) in full");
        unsynced.forEach(function(n) { mustSend[n.hash] = true; });
        build();
        return payload;
      });
    }

    // POST to a local-mode route with one automatic 409 retry (unknown hash
    // refs are resent as full content). Resolves to the raw Response.
    function localFetch(path, payload, makeBody) {
      function attempt(retried) {
        return fetch(baseURL() + path, {
          method: "POST",
          headers: headers(),
          body: JSON.stringify(makeBody(payload.tiddlers))
        }).then(function(r) {
          if (r.status === 409 && !retried) {
            return r.json().catch(function() { return {}; }).then(function(d) {
              var missing = (d.detail && d.detail.missing) || [];
              dbg("409 from " + path + " - resending " + missing.length + " note(s) in full");
              payload.resend(missing);
              return attempt(true);
            });
          }
          return r;
        });
      }
      return attempt(false);
    }

    function annotateLocal(data, payload) {
      if (payload.capped && data && !data.local_note) {
        data.local_note = "searched " + payload.tiddlers.length + " of " +
          payload.total + " notes";
      }
      return data;
    }

    // Read an OK SSE response, dispatching delta/done/error frames.
    function pumpSSE(r, handlers) {
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function handleFrame(frame) {
        var name = null, data = "";
        frame.split("\n").forEach(function(line) {
          if (line.indexOf("event: ") === 0) name = line.slice(7).trim();
          else if (line.indexOf("data: ") === 0) data += line.slice(6);
        });
        if (!name) return;
        var payload = {};
        try { payload = JSON.parse(data); } catch (e) {}
        if (name === "delta" && payload.text) {
          handlers.onDelta(payload.text);
        } else if (name === "done") {
          handlers.onDone(payload);
        } else if (name === "error") {
          var err = new Error(payload.message || "stream error");
          err.status = payload.status || 503;
          throw err;
        }
      }
      function pump() {
        return reader.read().then(function(step) {
          if (step.done) {
            if (buffer.trim()) handleFrame(buffer);
            return;
          }
          buffer += decoder.decode(step.value, {stream: true});
          var idx;
          while ((idx = buffer.indexOf("\n\n")) !== -1) {
            var frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            if (frame.trim()) handleFrame(frame);
          }
          return pump();
        });
      }
      return pump();
    }

    $tw.Familiar = {
      ask: function(question, filter, history) {
        var body = {question: question};
        if (history && history.length) body.history = history;
        applyAskOverrides(body);
        return localPayload(filter).then(function(p) {
          return localFetch("/ask", p, function(tiddlers) {
            body.tiddlers = tiddlers;
            return body;
          }).then(function(r) {
            if (!r.ok) return rejectWithDetail(r);
            return r.json().then(function(data) { return annotateLocal(data, p); });
          });
        });
      },
      // SSE stream of the answer: handlers.onDelta(text) per fragment, then
      // handlers.onDone({answer, sources, truncated}). Falls back to the
      // plain ask endpoint when the browser can't read response streams.
      askStream: function(question, filter, history, handlers) {
        var body = {question: question};
        if (history && history.length) body.history = history;
        applyAskOverrides(body);
        return localPayload(filter).then(function(p) {
          return localFetch("/ask/stream", p, function(tiddlers) {
            body.tiddlers = tiddlers;
            return body;
          }).then(function(r) {
            if (!r.ok) return rejectWithDetail(r);
            if (!r.body || !r.body.getReader) {
              return $tw.Familiar.ask(question, filter, history)
                .then(function(data) { handlers.onDone(data); });
            }
            return pumpSSE(r, {
              onDelta: handlers.onDelta,
              onDone: function(data) { handlers.onDone(annotateLocal(data, p)); }
            });
          });
        });
      },
      // One-shot generation command over a single tiddler (summarize / tags /
      // title / tasks). The note is rendered to plain text HERE — the gateway
      // can't render a wiki it has no session for — with a raw-text fallback
      // for data tiddlers, matching the server-side notebook behaviour.
      generate: function(title, command) {
        var t = $tw.wiki.getTiddler(title);
        if (!t) {
          var missing = new Error("Tiddler '" + title + "' not found");
          missing.status = 404;
          return Promise.reject(missing);
        }
        var text = "";
        try { text = ($tw.wiki.renderTiddler("text/plain", title) || "").trim(); } catch (e) {}
        if (!text) text = String(t.fields.text || "").trim();
        return this.generateText(title, command, text);
      },
      // Same /generate call, but over already-assembled text (e.g. a chat
      // transcript) instead of a single tiddler's rendered body.
      generateText: function(title, command, text) {
        var body = {title: title, command: command, text: String(text || "").slice(0, LOCAL_MAX_TEXT)};
        if (command === "tags") {
          body.vocabulary = $tw.wiki.filterTiddlers("[tags[]]");
        }
        return fetch(baseURL() + "/generate", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify(body)
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          return r.json();
        });
      },
      related: function(title, k) {
        var t = $tw.wiki.getTiddler(title);
        if (!t) {
          var missing = new Error("Tiddler '" + title + "' not found");
          missing.status = 404;
          return Promise.reject(missing);
        }
        var tags = t.fields.tags ? $tw.utils.stringifyList(t.fields.tags) : "";
        var target = {
          title: title,
          text: String(t.fields.text || "").slice(0, LOCAL_MAX_TEXT),
          fields: tags ? {tags: tags} : {}
        };
        return localPayload(null).then(function(p) {
          return localFetch("/related", p, function(tiddlers) {
            return applyMaxTiddlers({target: target, tiddlers: tiddlers, k: k || 5});
          });
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          return r.json();
        });
      },
      // Ranked semantic search over the notes matched by `filter` (or the
      // whole wiki). No generation — resolves to {results:[{title,score,
      // snippet}], truncated, cache}, with the same "searched N of M" notice
      // as ask when the corpus is capped.
      search: function(query, filter, k) {
        return localPayload(filter).then(function(p) {
          return localFetch("/search", p, function(tiddlers) {
            return applyMaxTiddlers({query: query, tiddlers: tiddlers, k: k || 10});
          }).then(function(r) {
            if (!r.ok) return rejectWithDetail(r);
            return r.json().then(function(data) { return annotateLocal(data, p); });
          });
        });
      },
      // Force a full corpus sync + status refresh (settings "Sync now" button).
      syncNow: syncNow
    };

    function askErrorMessage(err) {
      if (!err.status) {
        return "Cannot reach the gateway - is it running at " + baseURL() + "?";
      }
      if (err.status === 503) {
        return err.message;
      }
      if (err.status === 403) {
        return "Authentication failed - check the API key in the gateway config tiddler.";
      }
      return "Error (" + err.status + "): " + err.message;
    }

    // Keep the chat log pinned to the newest message (SMS-style). Runs after
    // TW's own change-driven refresh so the new DOM is in place.
    var scrollPending = false;
    function scrollChatLogs() {
      scrollPending = false;
      var logs = document.querySelectorAll(".fam-chat-log");
      for (var i = 0; i < logs.length; i++) {
        logs[i].scrollTop = logs[i].scrollHeight;
      }
    }
    $tw.wiki.addEventListener("change", function(changes) {
      if (scrollPending) return;
      for (var title in changes) {
        if (title.indexOf(CHAT_PREFIX) === 0 ||
            title.indexOf("/turn/") !== -1 ||
            title === "$:/state/familiar/answer" ||
            title === "$:/state/familiar/asking") {
          scrollPending = true;
          setTimeout(scrollChatLogs, 50);
          return;
        }
      }
    });

    // Shared ask-with-streaming flow: the sidebar and the in-note composer
    // differ only in where their state lives and where the assistant turn
    // lands (opts.note null = the temp sidebar transcript).
    function streamAsk(opts) {
      var acc = "";
      var flushTimer = null;
      function flush() {
        flushTimer = null;
        setState(opts.answerState, acc, "text/markdown");
      }
      $tw.Familiar.askStream(opts.question, opts.filter || null, opts.history, {
        onDelta: function(text) {
          acc += text;
          if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
        },
        onDone: function(data) {
          if (flushTimer) clearTimeout(flushTimer);
          dbg("ask ok; answer length=" + ((data.answer||"").length) + " sources=" + ((data.sources||[]).length));
          appendTurn("assistant", data.answer || "(no answer)", data.sources || [], opts.note);
          setState(opts.answerState, "");
          setState(opts.askingState, "no");
          if (opts.onDone) opts.onDone(data);
        }
      }).catch(function(err) {
        if (flushTimer) clearTimeout(flushTimer);
        var msg = askErrorMessage(err);
        dbg("ask FAILED (" + (err.status || "network") + "): " + err.message);
        setState(opts.answerState, "//" + msg + "//", "text/vnd.tiddlywiki");
        setState(opts.askingState, "no");
      });
    }

    $tw.rootWidget.addEventListener("tm-ask-ai", function() {
      if (($tw.wiki.getTiddlerText("$:/state/familiar/asking") || "") === "yes") return;
      var question = ($tw.wiki.getTiddlerText("$:/temp/volatile/familiar/question") || "").trim();
      var filter   = ($tw.wiki.getTiddlerText("$:/temp/volatile/familiar/filter")   || "").trim();
      dbg("tm-ask-ai fired; question=" + JSON.stringify(question) + " filter=" + JSON.stringify(filter));
      if (!question) {
        setState("$:/state/familiar/answer", "//Type a question first.//");
        return;
      }
      var history = chatHistory();
      appendTurn("user", question);
      setState("$:/temp/volatile/familiar/question", "");
      setState("$:/state/familiar/asking",  "yes");
      setState("$:/state/familiar/answer",  "", "text/markdown");
      setState("$:/state/familiar/sources", "");
      streamAsk({
        question: question,
        filter: filter,
        history: history,
        note: boundChatNote(),
        answerState: "$:/state/familiar/answer",
        askingState: "$:/state/familiar/asking",
        onDone: function(data) {
          var sources = (data.sources || []).map(function(s) { return "* [[" + s + "]]"; }).join("\n");
          if (data.local_note) {
            sources += (sources ? "\n" : "") + "//(" + data.local_note + " — narrow the filter to cover the rest)//";
          }
          setState("$:/state/familiar/sources", sources);
        }
      });
    });

    // In-note chat: any tiddler tagged ai-chat renders its transcript plus a
    // composer (ViewTemplate). Question/asking/answer state is per note, so
    // several note chats and the sidebar can stream independently; turns are
    // recorded under the note's system turn prefix like any saved chat.
    $tw.rootWidget.addEventListener("tm-ask-ai-note", function(event) {
      var note = event.param || "";
      if (!note || !$tw.wiki.tiddlerExists(note)) return;
      var qState      = "$:/temp/volatile/familiar/note-question/" + note;
      var askingState = "$:/state/familiar/note-asking/" + note;
      var answerState = "$:/state/familiar/note-answer/" + note;
      if (($tw.wiki.getTiddlerText(askingState) || "") === "yes") return;
      var question = ($tw.wiki.getTiddlerText(qState) || "").trim();
      dbg("tm-ask-ai-note fired; note=" + JSON.stringify(note) + " question=" + JSON.stringify(question));
      if (!question) return;
      var history = chatHistoryFor(turnPrefixFor(note));
      appendTurn("user", question, null, note);
      setState(qState, "");
      setState(askingState, "yes");
      setState(answerState, "", "text/markdown");
      streamAsk({
        question: question,
        history: history,
        note: note,
        answerState: answerState,
        askingState: askingState
      });
    });

    // Persist the current temp transcript as a real note tagged CHAT_TAG:
    // turns move under the note's system turn prefix (persisted, but hidden
    // from Recent) and the panel binds to the note so later turns land there.
    $tw.rootWidget.addEventListener("tm-save-chat", function() {
      if (boundChatNote()) return; // already persisting into a note
      var titles = chatTurnTitles(CHAT_PREFIX);
      if (!titles.length) return;
      var firstQuestion = "";
      titles.some(function(t) {
        var tid = $tw.wiki.getTiddler(t);
        if (tid.fields.role !== "assistant") {
          firstQuestion = tid.fields.text || "";
          return true;
        }
        return false;
      });
      var noteTitle = chatNoteTitleFor(firstQuestion);
      $tw.wiki.addTiddler(new $tw.Tiddler($tw.wiki.getCreationFields(), {
        title: noteTitle,
        tags: [CHAT_TAG],
        text: ""
      }, $tw.wiki.getModificationFields()));
      titles.forEach(function(t, i) {
        var tid = $tw.wiki.getTiddler(t);
        $tw.wiki.addTiddler(new $tw.Tiddler(
          $tw.wiki.getCreationFields(), tid.fields, {
            title: turnPrefixFor(noteTitle) + padTurn(i + 1),
            tags: [CHAT_TURN_TAG]
          }, $tw.wiki.getModificationFields()));
        $tw.wiki.deleteTiddler(t);
      });
      setState(CHAT_NOTE_STATE, noteTitle);
      scheduleDigest(noteTitle);
      dbg("chat saved to " + JSON.stringify(noteTitle) + " (" + titles.length + " turns)");
    });

    // Propose a fresh note title from the template (also the "reroll" tap —
    // every dispatch re-expands {name}/{date}/{time}).
    $tw.rootWidget.addEventListener("tm-new-chat-title", function() {
      setState(NEW_TITLE_STATE, uniqueTitle(expandTitleTemplate()));
    });

    // Create a chat note up front (before any question), bind the panel to it
    // and open it in the story. Any unsaved temp transcript is discarded —
    // this starts a fresh conversation that persists from turn one.
    $tw.rootWidget.addEventListener("tm-new-chat-note", function() {
      var title = sanitizeTitle($tw.wiki.getTiddlerText(NEW_TITLE_STATE) || "");
      if (!title) title = expandTitleTemplate();
      title = uniqueTitle(title);
      $tw.wiki.addTiddler(new $tw.Tiddler($tw.wiki.getCreationFields(), {
        title: title,
        tags: [CHAT_TAG],
        text: ""
      }, $tw.wiki.getModificationFields()));
      chatTurnTitles(CHAT_PREFIX).forEach(function(t) { $tw.wiki.deleteTiddler(t); });
      setState(CHAT_NOTE_STATE, title);
      setState("$:/state/familiar/answer", "");
      setState("$:/state/familiar/sources", "");
      setState(NEW_TITLE_STATE, "");
      setState(NEW_NOTE_OPEN_STATE, "no");
      if ($tw.wiki.addToStory) {
        $tw.wiki.addToStory(title);
        $tw.wiki.addToHistory(title);
      }
      dbg("new chat note " + JSON.stringify(title));
    });

    // Bind the panel to a previously saved chat note (its turns become the
    // transcript + history). Discards any unsaved temp transcript.
    $tw.rootWidget.addEventListener("tm-resume-chat", function(event) {
      var title = event.param || "";
      if (!title || !$tw.wiki.tiddlerExists(title)) return;
      chatTurnTitles(CHAT_PREFIX).forEach(function(t) { $tw.wiki.deleteTiddler(t); });
      setState(CHAT_NOTE_STATE, title);
      setState("$:/state/familiar/answer", "");
      setState("$:/state/familiar/sources", "");
      dbg("resumed chat " + JSON.stringify(title));
    });

    $tw.rootWidget.addEventListener("tm-summarize-tiddler", function(event) {
      var title = event.param || "";
      if (!title) return;
      dbg("tm-summarize-tiddler fired; title=" + JSON.stringify(title));
      setState("$:/state/familiar/summarizing", title);
      $tw.Familiar.generate(title, "summarize").then(function(data) {
        var t = $tw.wiki.getTiddler(title);
        $tw.wiki.addTiddler(new $tw.Tiddler(t, {summary: data.result || ""}));
        setState("$:/state/familiar/summarizing", "");
        dbg("summarize ok; length=" + ((data.result||"").length));
      }).catch(function(err) {
        setState("$:/state/familiar/summarizing", "");
        setState("$:/state/familiar/summary-error", askErrorMessage(err));
        dbg("summarize FAILED: " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-suggest-tags", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/familiar/tags/" + title;
      dbg("tm-suggest-tags fired; title=" + JSON.stringify(title));
      setState("$:/state/familiar/tags-loading", title);
      $tw.Familiar.generate(title, "tags").then(function(data) {
        $tw.wiki.addTiddler(new $tw.Tiddler({
          title: stateTitle,
          text: $tw.utils.stringifyList(data.tags || [])
        }));
        setState("$:/state/familiar/tags-loading", "");
        dbg("tags ok; count=" + ((data.tags || []).length));
      }).catch(function(err) {
        $tw.wiki.addTiddler(new $tw.Tiddler({
          title: stateTitle, text: "", error: askErrorMessage(err)
        }));
        setState("$:/state/familiar/tags-loading", "");
        dbg("tags FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-extract-tasks", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/familiar/tasks/" + title;
      dbg("tm-extract-tasks fired; title=" + JSON.stringify(title));
      setState("$:/state/familiar/tasks-loading", title);
      $tw.Familiar.generate(title, "tasks").then(function(data) {
        setState(stateTitle, data.result || "(no tasks found)", "text/markdown");
        setState("$:/state/familiar/tasks-loading", "");
        dbg("tasks ok; length=" + ((data.result||"").length));
      }).catch(function(err) {
        setState(stateTitle, "//" + askErrorMessage(err) + "//");
        setState("$:/state/familiar/tasks-loading", "");
        dbg("tasks FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-related-notes", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/familiar/related/" + title;
      dbg("tm-related-notes fired; title=" + JSON.stringify(title));
      setState("$:/state/familiar/related-loading", title);
      $tw.Familiar.related(title, cfgInt("RelatedCount") || 5).then(function(data) {
        var items = (data.related || []).map(function(r) { return "* [[" + r.title + "]]"; }).join("\n");
        if (!items) {
          items = "//No related notes found" + (data.truncated ? " yet — the index is still warming, try again//" : ".//");
        }
        setState(stateTitle, items);
        setState("$:/state/familiar/related-loading", "");
        dbg("related ok; count=" + ((data.related || []).length));
      }).catch(function(err) {
        setState(stateTitle, "//" + askErrorMessage(err) + "//");
        setState("$:/state/familiar/related-loading", "");
        dbg("related FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    // --- "Insert into note": materialise AI output as editable note body ---
    // The info-dropdown tab previews summary/tasks; these handlers write them
    // into the note text as real wikitext (or Markdown, matching the note's
    // own type) that the user then owns and edits. Idempotent-ish: a second
    // insert is skipped when the same content is already present in the body.

    function noteIsMarkdown(t) {
      var ty = String((t.fields && t.fields.type) || "").toLowerCase();
      return ty === "text/markdown" || ty === "text/x-markdown";
    }

    // Replace the note's text field, bumping modified so the syncer flushes it.
    function setNoteText(t, newText) {
      $tw.wiki.addTiddler(new $tw.Tiddler(
        t, {text: newText}, $tw.wiki.getModificationFields()));
    }

    $tw.rootWidget.addEventListener("tm-insert-summary", function(event) {
      var title = event.param || "";
      if (!title) return;
      var t = $tw.wiki.getTiddler(title);
      if (!t) return;
      var summary = String(t.fields.summary || "").trim();
      if (!summary) return;
      var body = String(t.fields.text || "");
      if (body.indexOf(summary) !== -1) {
        dbg("insert-summary skipped; already in body: " + JSON.stringify(title));
        return;
      }
      var block = noteIsMarkdown(t)
        ? "> **AI summary:** " + summary.replace(/\n/g, "\n> ")
        : "<<<.tc-quote\n''AI summary''\n\n" + summary + "\n<<<";
      setNoteText(t, block + (body ? "\n\n" + body : "\n"));
      dbg("inserted summary into " + JSON.stringify(title));
    });

    $tw.rootWidget.addEventListener("tm-insert-tasks", function(event) {
      var title = event.param || "";
      if (!title) return;
      var t = $tw.wiki.getTiddler(title);
      if (!t) return;
      var tasks = String(
        $tw.wiki.getTiddlerText("$:/temp/familiar/tasks/" + title) || "").trim();
      if (!tasks || tasks === "(no tasks found)") return;
      // Normalise the gateway's Markdown bullets to the note's own list syntax.
      var md = noteIsMarkdown(t);
      var bullet = md ? "- " : "* ";
      var items = tasks.split("\n").map(function(line) {
        var m = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
        return m ? bullet + m[1] : line;
      }).join("\n");
      var block = (md ? "## Tasks" : "!! Tasks") + "\n\n" + items;
      var body = String(t.fields.text || "");
      if (body.indexOf(items) !== -1) {
        dbg("insert-tasks skipped; already in body: " + JSON.stringify(title));
        return;
      }
      setNoteText(t, (body ? body.replace(/\s*$/, "") + "\n\n" : "") + block + "\n");
      dbg("inserted tasks into " + JSON.stringify(title));
    });

    // Sidebar semantic search: rank the notes (optionally filtered) by the
    // query and render a scored, snippeted list — no generation. Shares the
    // composer's question/filter state with ask; the panel's mode toggle
    // decides which message the send button fires.
    $tw.rootWidget.addEventListener("tm-familiar-search", function() {
      if (($tw.wiki.getTiddlerText("$:/state/familiar/searching") || "") === "yes") return;
      var query  = ($tw.wiki.getTiddlerText("$:/temp/volatile/familiar/question") || "").trim();
      var filter = ($tw.wiki.getTiddlerText("$:/temp/volatile/familiar/filter")   || "").trim();
      dbg("tm-familiar-search fired; query=" + JSON.stringify(query) + " filter=" + JSON.stringify(filter));
      if (!query) {
        setState("$:/state/familiar/search-results", "//Type something to search for.//");
        return;
      }
      clearSearchResults();
      setState("$:/state/familiar/searching", "yes");
      setState("$:/state/familiar/search-results", "");
      $tw.Familiar.search(query, filter, cfgInt("SearchResultCount") || 10).then(function(data) {
        var results = data.results || [];
        // Each result is its own tiddler; the panel renders them with a $list
        // widget (repeating one parsed template — a transcluded string of many
        // sibling <div> blocks only renders the first couple). Snippets show
        // via <$view field> so note text is never re-parsed as wikitext.
        results.forEach(function(r, i) {
          $tw.wiki.addTiddler(new $tw.Tiddler({
            title: SEARCH_RESULT_PREFIX + padTurn(i + 1),
            caption: r.title,
            score: String(Math.round((r.score || 0) * 100)),
            snippet: (r.snippet || "").replace(/\s+/g, " ").trim()
          }));
        });
        var note = "";
        if (!results.length) {
          note = data.truncated
            ? "//No matches yet — the index is still warming, try again.//"
            : "//No matches.//";
        } else if (data.local_note) {
          note = "//(" + data.local_note + " — narrow the filter to cover the rest)//";
        }
        setState("$:/state/familiar/search-results", note);
        setState("$:/state/familiar/searching", "");
        dbg("search ok; results=" + results.length);
      }).catch(function(err) {
        setState("$:/state/familiar/search-results", "//" + askErrorMessage(err) + "//");
        setState("$:/state/familiar/searching", "");
        dbg("search FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    // Settings "Sync now": force a full sync. Ignored while one is running so a
    // double-click can't launch overlapping warms.
    $tw.rootWidget.addEventListener("tm-familiar-sync", function() {
      if (($tw.wiki.getTiddlerText(SYNC_STATUS) || "") === "syncing") return;
      dbg("manual sync requested");
      syncNow().catch(function(err) {
        dbg("manual sync failed (" + err.message + ") - asks degrade to full sends");
      });
    });

    startInitialScan();

    dbg("ready - gateway=" + baseURL() + " (local mode)" +
        " apiKey=" + (apiKey() ? "set(" + apiKey().length + ")" : "MISSING"));
  } catch (e) {
    dbg("STARTUP ERROR: " + (e && e.stack ? e.stack : e));
  }
};

})();
