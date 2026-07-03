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

    var CHAT_PREFIX = "$:/temp/ai-gateway/chat/";
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

    // --- rolling chat history: one tiddler per turn under CHAT_PREFIX ---

    function chatTurnTitles() {
      return $tw.wiki.filterTiddlers("[prefix[" + CHAT_PREFIX + "]sort[title]]");
    }

    function chatHistory() {
      return chatTurnTitles().map(function(title) {
        var t = $tw.wiki.getTiddler(title);
        return {
          role: t.fields.role === "assistant" ? "assistant" : "user",
          content: t.fields.text || ""
        };
      });
    }

    function appendTurn(role, content, sources) {
      var titles = chatTurnTitles();
      var last = titles.length
        ? parseInt(titles[titles.length - 1].slice(CHAT_PREFIX.length), 10) : 0;
      var fields = {
        title: CHAT_PREFIX + ("000000" + (last + 1)).slice(-6),
        text: content,
        role: role
      };
      if (role === "assistant") fields.type = "text/markdown";
      if (sources && sources.length) fields.sources = $tw.utils.stringifyList(sources);
      $tw.wiki.addTiddler(new $tw.Tiddler(fields));
      titles = chatTurnTitles();
      while (titles.length > MAX_TURNS) {
        $tw.wiki.deleteTiddler(titles.shift());
      }
    }

    $tw.TiddlyPWAGateway = {
      ask: function(question, filter, history) {
        var body = {question: question};
        if (filter) body.filter = filter;
        if (history && history.length) body.history = history;
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
        if (filter) body.filter = filter;
        if (history && history.length) body.history = history;
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

    $tw.rootWidget.addEventListener("tm-ask-ai", function() {
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

      var acc = "";
      var flushTimer = null;
      function flush() {
        flushTimer = null;
        setState("$:/state/ai-gateway/answer", acc, "text/markdown");
      }
      $tw.TiddlyPWAGateway.askStream(question, filter || null, history, {
        onDelta: function(text) {
          acc += text;
          if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
        },
        onDone: function(data) {
          if (flushTimer) clearTimeout(flushTimer);
          dbg("ask ok; answer length=" + ((data.answer||"").length) + " sources=" + ((data.sources||[]).length));
          appendTurn("assistant", data.answer || "(no answer)", data.sources || []);
          var sources = (data.sources || []).map(function(s) { return "* [[" + s + "]]"; }).join("\n");
          setState("$:/state/ai-gateway/answer",  "");
          setState("$:/state/ai-gateway/sources", sources);
          setState("$:/state/ai-gateway/asking",  "no");
        }
      }).catch(function(err) {
        if (flushTimer) clearTimeout(flushTimer);
        var msg = askErrorMessage(err);
        dbg("ask FAILED (" + (err.status || "network") + "): " + err.message);
        setState("$:/state/ai-gateway/answer",  "//" + msg + "//", "text/vnd.tiddlywiki");
        setState("$:/state/ai-gateway/asking",  "no");
      });
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

    dbg("ready - gateway=" + baseURL + " notebook=" + notebook + " apiKey=" + (apiKey ? "set(" + apiKey.length + ")" : "MISSING"));
  } catch (e) {
    dbg("STARTUP ERROR: " + (e && e.stack ? e.stack : e));
  }
};

})();
