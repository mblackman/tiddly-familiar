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

    // This wiki sends its own note content with every request (the gateway's
    // client-supplied-content routes), so everything works against any
    // TiddlyWiki — the gateway needs no notebook config or wiki credentials.
    // Budgets mirrored from the gateway (MAX_CLIENT_TIDDLERS etc. in main.py):
    // enforce here by dropping overflow so requests never 422.
    var LOCAL_MAX_TIDDLERS = 500;
    var LOCAL_MAX_TEXT = 50000;
    var LOCAL_MAX_TOTAL = 2000000;

    var CHAT_PREFIX = "$:/temp/familiar/chat/";
    var SEARCH_RESULT_PREFIX = "$:/temp/familiar/search/result/";
    var CHAT_NOTE_STATE = "$:/state/familiar/chat-note";
    var NEW_TITLE_STATE = "$:/state/familiar/new-chat-title";
    var NEW_NOTE_OPEN_STATE = "$:/state/familiar/new-note-open";
    var TITLE_TEMPLATE_DEFAULT = "AI Chat: {name}";
    var CHAT_TAG = "ai-chat";
    var CHAT_TURN_TAG = "ai-chat-turn";
    var MAX_TURNS = 10;
    var FLUSH_MS = 120; // throttle transcript re-renders while streaming

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
    // synced). "Save chat" moves the turns under a real note ("<note>/turn/")
    // and binds the panel to it via CHAT_NOTE_STATE, so every later turn is
    // written as a synced tiddler too.

    function boundChatNote() {
      var title = ($tw.wiki.getTiddlerText(CHAT_NOTE_STATE) || "").trim();
      return title && $tw.wiki.tiddlerExists(title) ? title : null;
    }

    function chatPrefix() {
      var note = boundChatNote();
      return note ? note + "/turn/" : CHAT_PREFIX;
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
      var prefix = note ? note + "/turn/" : CHAT_PREFIX;
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
        return;
      }
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
      titles = chatTurnTitles(prefix);
      while (titles.length > MAX_TURNS) {
        $tw.wiki.deleteTiddler(titles.shift());
      }
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
        hash: noteHash(title, text, tags)
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
    var PUSH_DEBOUNCE_MS = 5000;
    var SCAN_SLICE = 25;    // titles hashed per idle slice
    var SYNC_BATCH_NOTES = 100;
    var SYNC_BATCH_CHARS = 500000;
    var SYNC_STATUS = "$:/temp/familiar/sync-status";

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
        return pushTitles(rest);
      });
    }

    function armPushFlush() {
      if (pushTimer) clearTimeout(pushTimer);
      pushTimer = setTimeout(function() {
        pushTimer = null;
        pushTitles(Object.keys(pendingPush)).catch(function(err) {
          dbg("background push failed (" + err.message + ") - asks still work");
        });
      }, PUSH_DEBOUNCE_MS);
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
          delete syncState[title];
          delete pendingPush[title];
          continue;
        }
        var n = readNote(title);
        if (!n) continue;
        syncState[title] = {hash: n.hash, synced: false};
        pendingPush[title] = true;
        dirty = true;
      }
      if (dirty) armPushFlush();
    });

    // Hash the whole wiki once, in idle-time slices so startup never janks,
    // then warm the server: one /notes/check over everything, upload the
    // missing notes. After this, steady-state asks are pure hash refs with
    // no preflight and no hashing.
    function startInitialScan() {
      setState(SYNC_STATUS, "scanning");
      var titles = $tw.wiki.filterTiddlers("[!is[system]]");
      var i = 0;
      function idle(fn) {
        if (typeof requestIdleCallback === "function") requestIdleCallback(fn);
        else setTimeout(fn, 50);
      }
      function slice() {
        var end = Math.min(i + SCAN_SLICE, titles.length);
        for (; i < end; i++) {
          if (!syncState[titles[i]]) { // change handler may have beaten us
            var n = readNote(titles[i]);
            if (n) syncState[titles[i]] = {hash: n.hash, synced: false};
          }
        }
        if (i < titles.length) return idle(slice);
        warmServer();
      }
      idle(slice);
    }

    function warmServer() {
      var titles = Object.keys(syncState);
      var toCheck = titles.filter(function(t) { return !syncState[t].synced; });
      fetch(baseURL() + "/notes/check", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({hashes: toCheck.map(function(t) { return syncState[t].hash; })})
      }).then(function(r) {
        if (!r.ok) throw new Error("notes/check " + r.status);
        return r.json();
      }).then(function(d) {
        var missing = {};
        (d.missing || []).forEach(function(h) { missing[h] = true; });
        var toPush = [];
        toCheck.forEach(function(t) {
          if (missing[syncState[t].hash]) toPush.push(t);
          else syncState[t].synced = true;
        });
        return pushTitles(toPush).then(function() {
          setState(SYNC_STATUS, "ready");
          dbg("sync warm done - " + titles.length + " note(s) tracked, " +
              toPush.length + " pushed");
        });
      }).catch(function(err) {
        setState(SYNC_STATUS, "degraded");
        dbg("sync warm failed (" + err.message + ") - asks degrade to full sends");
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
      titles.slice(0, LOCAL_MAX_TIDDLERS).forEach(function(title) {
        var e = syncState[title];
        if (!e) { // not scanned yet (mid-startup) or a system tiddler
          var n = readNote(title);
          if (!n) return;
          e = syncState[title] = {hash: n.hash, synced: false};
        }
        notes.push({title: title, hash: e.hash});
      });
      var mustSend = {};
      var payload = {tiddlers: [], total: total, capped: total > LOCAL_MAX_TIDDLERS};

      function build() {
        var out = [], budget = 0;
        notes.forEach(function(n) {
          if (!mustSend[n.hash]) {
            out.push({hash: n.hash});
            return;
          }
          var full = readNote(n.title);
          if (!full) return;
          if (budget + full.text.length > LOCAL_MAX_TOTAL) {
            payload.capped = true; // over budget: drop, self-heals once others cache
            return;
          }
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
      return fetch(baseURL() + "/notes/check", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({hashes: unsynced.map(function(n) { return n.hash; })})
      }).then(function(r) {
        if (!r.ok) throw new Error("notes/check " + r.status);
        return r.json();
      }).then(function(d) {
        var missing = {};
        (d.missing || []).forEach(function(h) { missing[h] = true; });
        unsynced.forEach(function(n) {
          var e = syncState[n.title];
          if (missing[n.hash]) mustSend[n.hash] = true;
          else if (e && e.hash === n.hash) e.synced = true;
        });
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
        var body = {title: title, command: command, text: text.slice(0, LOCAL_MAX_TEXT)};
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
            return {target: target, tiddlers: tiddlers, k: k || 5};
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
            return {query: query, tiddlers: tiddlers, k: k || 10};
          }).then(function(r) {
            if (!r.ok) return rejectWithDetail(r);
            return r.json().then(function(data) { return annotateLocal(data, p); });
          });
        });
      }
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
      var question = ($tw.wiki.getTiddlerText("$:/state/familiar/question") || "").trim();
      var filter   = ($tw.wiki.getTiddlerText("$:/state/familiar/filter")   || "").trim();
      dbg("tm-ask-ai fired; question=" + JSON.stringify(question) + " filter=" + JSON.stringify(filter));
      if (!question) {
        setState("$:/state/familiar/answer", "//Type a question first.//");
        return;
      }
      var history = chatHistory();
      appendTurn("user", question);
      setState("$:/state/familiar/question", "");
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
    // recorded under "<note>/turn/NNNNNN" like any saved chat.
    $tw.rootWidget.addEventListener("tm-ask-ai-note", function(event) {
      var note = event.param || "";
      if (!note || !$tw.wiki.tiddlerExists(note)) return;
      var qState      = "$:/state/familiar/note-question/" + note;
      var askingState = "$:/state/familiar/note-asking/" + note;
      var answerState = "$:/state/familiar/note-answer/" + note;
      if (($tw.wiki.getTiddlerText(askingState) || "") === "yes") return;
      var question = ($tw.wiki.getTiddlerText(qState) || "").trim();
      dbg("tm-ask-ai-note fired; note=" + JSON.stringify(note) + " question=" + JSON.stringify(question));
      if (!question) return;
      var history = chatHistoryFor(note + "/turn/");
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
    // turns move to "<note>/turn/NNNNNN" (synced tiddlers) and the panel
    // binds to the note so the rest of the conversation persists too.
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
            title: noteTitle + "/turn/" + padTurn(i + 1),
            tags: [CHAT_TURN_TAG]
          }, $tw.wiki.getModificationFields()));
        $tw.wiki.deleteTiddler(t);
      });
      setState(CHAT_NOTE_STATE, noteTitle);
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
      $tw.Familiar.related(title, 5).then(function(data) {
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

    // Sidebar semantic search: rank the notes (optionally filtered) by the
    // query and render a scored, snippeted list — no generation. Shares the
    // composer's question/filter state with ask; the panel's mode toggle
    // decides which message the send button fires.
    $tw.rootWidget.addEventListener("tm-familiar-search", function() {
      if (($tw.wiki.getTiddlerText("$:/state/familiar/searching") || "") === "yes") return;
      var query  = ($tw.wiki.getTiddlerText("$:/state/familiar/question") || "").trim();
      var filter = ($tw.wiki.getTiddlerText("$:/state/familiar/filter")   || "").trim();
      dbg("tm-familiar-search fired; query=" + JSON.stringify(query) + " filter=" + JSON.stringify(filter));
      if (!query) {
        setState("$:/state/familiar/search-results", "//Type something to search for.//");
        return;
      }
      clearSearchResults();
      setState("$:/state/familiar/searching", "yes");
      setState("$:/state/familiar/search-results", "");
      $tw.Familiar.search(query, filter, 10).then(function(data) {
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

    startInitialScan();

    dbg("ready - gateway=" + baseURL() + " (local mode)" +
        " apiKey=" + (apiKey() ? "set(" + apiKey().length + ")" : "MISSING"));
  } catch (e) {
    dbg("STARTUP ERROR: " + (e && e.stack ? e.stack : e));
  }
};

})();
