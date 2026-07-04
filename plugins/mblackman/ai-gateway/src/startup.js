(function(){
"use strict";

exports.name = "tiddlypwa-gateway";
exports.platforms = ["browser"];
exports.after = ["startup"];
exports.before = ["render"];
exports.synchronous = true;

exports.startup = function() {
  function dbg(msg) {
    $tw.wiki.addTiddler(new $tw.Tiddler({
      title: "$:/temp/ai-gateway/debug",
      text: msg
    }));
    console.log("[ai-gateway]", msg);
  }

  try {
    var cfg = function(name) {
      return ($tw.wiki.getTiddlerText("$:/config/mblackman/ai-gateway/" + name) || "").trim();
    };

    var baseURL = cfg("GatewayURL") || "http://localhost:8787";
    var notebook = cfg("Notebook") || "default";
    var apiKey   = cfg("APIKey");
    // "notebook": the gateway re-fetches notes from a server-configured wiki.
    // "local": this wiki sends its own note content with each request, so ask
    // and related-notes work against any TiddlyWiki the gateway never heard of.
    var mode     = cfg("Mode") === "local" ? "local" : "notebook";

    // Budgets mirrored from the gateway (MAX_CLIENT_TIDDLERS etc. in main.py):
    // enforce here by dropping overflow so requests never 422.
    var LOCAL_MAX_TIDDLERS = 500;
    var LOCAL_MAX_TEXT = 50000;
    var LOCAL_MAX_TOTAL = 2000000;

    var CHAT_PREFIX = "$:/temp/ai-gateway/chat/";
    var CHAT_NOTE_STATE = "$:/state/ai-gateway/chat-note";
    var NEW_TITLE_STATE = "$:/state/ai-gateway/new-chat-title";
    var NEW_NOTE_OPEN_STATE = "$:/state/ai-gateway/new-note-open";
    var TITLE_TEMPLATE_DEFAULT = "AI Chat: {name}";
    var CHAT_TAG = "ai-chat";
    var CHAT_TURN_TAG = "ai-chat-turn";
    var MAX_TURNS = 10;
    var FLUSH_MS = 120; // throttle transcript re-renders while streaming

    function headers() {
      var h = {"Content-Type": "application/json"};
      if (apiKey) h["X-API-Key"] = apiKey;
      return h;
    }

    function setState(title, text, type) {
      var fields = {title: title, text: text};
      if (type) fields.type = type;
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
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
    // Template lives in $:/config/mblackman/ai-gateway/ChatNoteTemplate;
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

    // Resolve the filter against this wiki. Only `tags` goes in fields — it's
    // the only field the gateway's keyword scoring reads. Text is truncated
    // at the same limit the server enforces, and hashed post-truncation so
    // both sides hash identical content.
    function collectLocalTiddlers(filter) {
      var titles = $tw.wiki.filterTiddlers((filter || "").trim() || "[!is[system]]");
      var notes = titles.slice(0, LOCAL_MAX_TIDDLERS).map(function(title) {
        var t = $tw.wiki.getTiddler(title);
        var tags = (t && t.fields.tags) ? $tw.utils.stringifyList(t.fields.tags) : "";
        var text = String((t && t.fields.text) || "").slice(0, LOCAL_MAX_TEXT);
        return {
          title: title,
          text: text,
          fields: tags ? {tags: tags} : {},
          hash: noteHash(title, text, tags)
        };
      });
      return {notes: notes, total: titles.length, capped: titles.length > LOCAL_MAX_TIDDLERS};
    }

    // Build the minimized payload: ask /notes/check which hashes the gateway
    // is missing, send those in full and the rest as {hash} refs. If the
    // preflight fails, degrade to sending everything — the cache is only an
    // optimization. `resend(hashes)` upgrades refs to full content for the
    // 409 retry (a note evicted between check and ask).
    function localPayload(filter) {
      var collected = collectLocalTiddlers(filter);
      var mustSend = {};
      var payload = {
        tiddlers: [],
        total: collected.total,
        capped: collected.capped,
        resend: function(hashes) {
          (hashes || []).forEach(function(h) { mustSend[h] = true; });
          var out = [], budget = 0;
          collected.notes.forEach(function(n) {
            if (!mustSend[n.hash]) {
              out.push({hash: n.hash});
            } else if (budget + n.text.length <= LOCAL_MAX_TOTAL) {
              budget += n.text.length;
              out.push({title: n.title, text: n.text, fields: n.fields});
            } else {
              payload.capped = true; // over budget: drop, self-heals once others cache
            }
          });
          payload.tiddlers = out;
          return out;
        }
      };
      return fetch(baseURL + "/notes/check", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({hashes: collected.notes.map(function(n) { return n.hash; })})
      }).then(function(r) {
        if (!r.ok) throw new Error("notes/check " + r.status);
        return r.json();
      }).catch(function(err) {
        dbg("notes/check unavailable (" + err.message + ") - sending full content");
        return {missing: collected.notes.map(function(n) { return n.hash; })};
      }).then(function(d) {
        payload.resend(d.missing || []);
        return payload;
      });
    }

    // POST to a local-mode route with one automatic 409 retry (unknown hash
    // refs are resent as full content). Resolves to the raw Response.
    function localFetch(path, payload, makeBody) {
      function attempt(retried) {
        return fetch(baseURL + path, {
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

    $tw.TiddlyPWAGateway = {
      ask: function(question, filter, history) {
        var body = {question: question};
        if (history && history.length) body.history = history;
        if (mode === "local") {
          return localPayload(filter).then(function(p) {
            return localFetch("/ask", p, function(tiddlers) {
              body.tiddlers = tiddlers;
              return body;
            });
          }).then(function(r) {
            if (!r.ok) return rejectWithDetail(r);
            return r.json();
          });
        }
        if (filter) body.filter = filter;
        return fetch(baseURL + "/notebooks/" + notebook + "/ask", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify(body)
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          return r.json();
        });
      },
      // SSE stream of the answer: handlers.onDelta(text) per fragment, then
      // handlers.onDone({answer, sources, truncated}). Falls back to the
      // plain ask endpoint when the browser can't read response streams.
      askStream: function(question, filter, history, handlers) {
        var body = {question: question};
        if (history && history.length) body.history = history;
        if (mode === "local") {
          return localPayload(filter).then(function(p) {
            return localFetch("/ask/stream", p, function(tiddlers) {
              body.tiddlers = tiddlers;
              return body;
            }).then(function(r) {
              if (!r.ok) return rejectWithDetail(r);
              if (!r.body || !r.body.getReader) {
                // ask() is mode-aware, so the fallback stays local too
                return $tw.TiddlyPWAGateway.ask(question, filter, history)
                  .then(function(data) { handlers.onDone(annotateLocal(data, p)); });
              }
              return pumpSSE(r, {
                onDelta: handlers.onDelta,
                onDone: function(data) { handlers.onDone(annotateLocal(data, p)); }
              });
            });
          });
        }
        if (filter) body.filter = filter;
        return fetch(baseURL + "/notebooks/" + notebook + "/ask/stream", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify(body)
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          if (!r.body || !r.body.getReader) {
            return $tw.TiddlyPWAGateway.ask(question, filter, history)
              .then(function(data) { handlers.onDone(data); });
          }
          return pumpSSE(r, handlers);
        });
      },
      // One-shot generation command over a single tiddler (summarize / tags /
      // title / tasks) — no retrieval round-trip.
      generate: function(title, command) {
        return fetch(baseURL + "/notebooks/" + notebook + "/generate", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({title: title, command: command})
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          return r.json();
        });
      },
      writeTiddler: function(title, text, fields) {
        var body = {title: title, text: text || "", fields: fields || {}};
        return fetch(baseURL + "/notebooks/" + notebook + "/tiddler", {
          method: "PUT", headers: headers(), body: JSON.stringify(body)
        }).then(function(r) { return r.ok; });
      },
      getTiddler: function(title) {
        return fetch(baseURL + "/notebooks/" + notebook + "/tiddler?title=" + encodeURIComponent(title), {
          headers: headers()
        }).then(function(r) { return r.ok ? r.json() : null; });
      },
      search: function(filter) {
        return fetch(baseURL + "/notebooks/" + notebook + "/tiddlers?filter=" + encodeURIComponent(filter), {
          headers: headers()
        }).then(function(r) { return r.json(); }).then(function(d) { return d.titles || d.tiddlers || []; });
      },
      related: function(title, k) {
        if (mode === "local") {
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
        }
        return fetch(baseURL + "/notebooks/" + notebook + "/related?title=" + encodeURIComponent(title) + "&k=" + (k || 5), {
          headers: headers()
        }).then(function(r) {
          if (!r.ok) return rejectWithDetail(r);
          return r.json();
        });
      }
    };

    function askErrorMessage(err) {
      if (!err.status) {
        return "Cannot reach the gateway - is it running at " + baseURL + "?";
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
      var logs = document.querySelectorAll(".ai-gw-chat-log");
      for (var i = 0; i < logs.length; i++) {
        logs[i].scrollTop = logs[i].scrollHeight;
      }
    }
    $tw.wiki.addEventListener("change", function(changes) {
      if (scrollPending) return;
      for (var title in changes) {
        if (title.indexOf(CHAT_PREFIX) === 0 ||
            title.indexOf("/turn/") !== -1 ||
            title === "$:/state/ai-gateway/answer" ||
            title === "$:/state/ai-gateway/asking") {
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
      $tw.TiddlyPWAGateway.askStream(opts.question, opts.filter || null, opts.history, {
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
      if (($tw.wiki.getTiddlerText("$:/state/ai-gateway/asking") || "") === "yes") return;
      var question = ($tw.wiki.getTiddlerText("$:/state/ai-gateway/question") || "").trim();
      var filter   = ($tw.wiki.getTiddlerText("$:/state/ai-gateway/filter")   || "").trim();
      dbg("tm-ask-ai fired; question=" + JSON.stringify(question) + " filter=" + JSON.stringify(filter));
      if (!question) {
        setState("$:/state/ai-gateway/answer", "//Type a question first.//");
        return;
      }
      var history = chatHistory();
      appendTurn("user", question);
      setState("$:/state/ai-gateway/question", "");
      setState("$:/state/ai-gateway/asking",  "yes");
      setState("$:/state/ai-gateway/answer",  "", "text/markdown");
      setState("$:/state/ai-gateway/sources", "");
      streamAsk({
        question: question,
        filter: filter,
        history: history,
        note: boundChatNote(),
        answerState: "$:/state/ai-gateway/answer",
        askingState: "$:/state/ai-gateway/asking",
        onDone: function(data) {
          var sources = (data.sources || []).map(function(s) { return "* [[" + s + "]]"; }).join("\n");
          if (data.local_note) {
            sources += (sources ? "\n" : "") + "//(" + data.local_note + " — narrow the filter to cover the rest)//";
          }
          setState("$:/state/ai-gateway/sources", sources);
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
      var qState      = "$:/state/ai-gateway/note-question/" + note;
      var askingState = "$:/state/ai-gateway/note-asking/" + note;
      var answerState = "$:/state/ai-gateway/note-answer/" + note;
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
      setState("$:/state/ai-gateway/answer", "");
      setState("$:/state/ai-gateway/sources", "");
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
      setState("$:/state/ai-gateway/answer", "");
      setState("$:/state/ai-gateway/sources", "");
      dbg("resumed chat " + JSON.stringify(title));
    });

    $tw.rootWidget.addEventListener("tm-summarize-tiddler", function(event) {
      var title = event.param || "";
      if (!title) return;
      dbg("tm-summarize-tiddler fired; title=" + JSON.stringify(title));
      setState("$:/state/ai-gateway/summarizing", title);
      $tw.TiddlyPWAGateway.generate(title, "summarize").then(function(data) {
        var t = $tw.wiki.getTiddler(title);
        $tw.wiki.addTiddler(new $tw.Tiddler(t, {summary: data.result || ""}));
        setState("$:/state/ai-gateway/summarizing", "");
        dbg("summarize ok; length=" + ((data.result||"").length));
      }).catch(function(err) {
        setState("$:/state/ai-gateway/summarizing", "");
        setState("$:/state/ai-gateway/summary-error", askErrorMessage(err));
        dbg("summarize FAILED: " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-suggest-tags", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/ai-gateway/tags/" + title;
      dbg("tm-suggest-tags fired; title=" + JSON.stringify(title));
      setState("$:/state/ai-gateway/tags-loading", title);
      $tw.TiddlyPWAGateway.generate(title, "tags").then(function(data) {
        $tw.wiki.addTiddler(new $tw.Tiddler({
          title: stateTitle,
          text: $tw.utils.stringifyList(data.tags || [])
        }));
        setState("$:/state/ai-gateway/tags-loading", "");
        dbg("tags ok; count=" + ((data.tags || []).length));
      }).catch(function(err) {
        $tw.wiki.addTiddler(new $tw.Tiddler({
          title: stateTitle, text: "", error: askErrorMessage(err)
        }));
        setState("$:/state/ai-gateway/tags-loading", "");
        dbg("tags FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-extract-tasks", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/ai-gateway/tasks/" + title;
      dbg("tm-extract-tasks fired; title=" + JSON.stringify(title));
      setState("$:/state/ai-gateway/tasks-loading", title);
      $tw.TiddlyPWAGateway.generate(title, "tasks").then(function(data) {
        setState(stateTitle, data.result || "(no tasks found)", "text/markdown");
        setState("$:/state/ai-gateway/tasks-loading", "");
        dbg("tasks ok; length=" + ((data.result||"").length));
      }).catch(function(err) {
        setState(stateTitle, "//" + askErrorMessage(err) + "//");
        setState("$:/state/ai-gateway/tasks-loading", "");
        dbg("tasks FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    $tw.rootWidget.addEventListener("tm-related-notes", function(event) {
      var title = event.param || "";
      if (!title) return;
      var stateTitle = "$:/temp/ai-gateway/related/" + title;
      dbg("tm-related-notes fired; title=" + JSON.stringify(title));
      setState("$:/state/ai-gateway/related-loading", title);
      $tw.TiddlyPWAGateway.related(title, 5).then(function(data) {
        var items = (data.related || []).map(function(r) { return "* [[" + r.title + "]]"; }).join("\n");
        if (!items) {
          items = "//No related notes found" + (data.truncated ? " yet — the index is still warming, try again//" : ".//");
        }
        setState(stateTitle, items);
        setState("$:/state/ai-gateway/related-loading", "");
        dbg("related ok; count=" + ((data.related || []).length));
      }).catch(function(err) {
        setState(stateTitle, "//" + askErrorMessage(err) + "//");
        setState("$:/state/ai-gateway/related-loading", "");
        dbg("related FAILED (" + (err.status || "network") + "): " + err.message);
      });
    });

    dbg("ready - gateway=" + baseURL + " mode=" + mode +
        (mode === "local" ? "" : " notebook=" + notebook) +
        " apiKey=" + (apiKey ? "set(" + apiKey.length + ")" : "MISSING"));
  } catch (e) {
    dbg("STARTUP ERROR: " + (e && e.stack ? e.stack : e));
  }
};

})();
