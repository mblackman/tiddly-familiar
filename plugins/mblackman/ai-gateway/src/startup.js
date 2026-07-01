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

    function headers() {
      var h = {"Content-Type": "application/json"};
      if (apiKey) h["X-API-Key"] = apiKey;
      return h;
    }

    function setState(title, text) {
      $tw.wiki.addTiddler(new $tw.Tiddler({title: title, text: text}));
    }

    $tw.TiddlyPWAGateway = {
      ask: function(question, filter) {
        var body = {question: question};
        if (filter) body.filter = filter;
        return fetch(baseURL + "/notebooks/" + notebook + "/ask", {
          method: "POST",
          headers: headers(),
          body: JSON.stringify(body)
        }).then(function(r) {
          if (!r.ok) {
            return r.json().catch(function() { return {}; }).then(function(errBody) {
              var err = new Error(errBody.detail || ("HTTP " + r.status));
              err.status = r.status;
              throw err;
            });
          }
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
      setState("$:/state/ai-gateway/asking",  "yes");
      setState("$:/state/ai-gateway/status",  "Thinking...");
      setState("$:/state/ai-gateway/answer",  "");
      setState("$:/state/ai-gateway/sources", "");
      $tw.TiddlyPWAGateway.ask(question, filter || null).then(function(data) {
        dbg("ask ok; answer length=" + ((data.answer||"").length) + " sources=" + ((data.sources||[]).length));
        setState("$:/state/ai-gateway/answer",  data.answer || "(no answer)");
        var sources = (data.sources || []).map(function(s) { return "* [[" + s + "]]"; }).join("\n");
        setState("$:/state/ai-gateway/sources", sources);
        setState("$:/state/ai-gateway/asking",  "no");
        setState("$:/state/ai-gateway/status",  "");
      }).catch(function(err) {
        var msg = askErrorMessage(err);
        dbg("ask FAILED (" + (err.status || "network") + "): " + err.message);
        setState("$:/state/ai-gateway/answer",  "//" + msg + "//");
        setState("$:/state/ai-gateway/asking",  "no");
        setState("$:/state/ai-gateway/status",  "");
      });
    });

    $tw.rootWidget.addEventListener("tm-summarize-tiddler", function(event) {
      var title = event.param || "";
      if (!title) return;
      dbg("tm-summarize-tiddler fired; title=" + JSON.stringify(title));
      setState("$:/state/ai-gateway/summarizing", title);
      $tw.TiddlyPWAGateway.ask(
        "Summarize this note in 2-3 sentences.",
        "[title[" + title + "]]"
      ).then(function(data) {
        var t = $tw.wiki.getTiddler(title);
        $tw.wiki.addTiddler(new $tw.Tiddler(t, {summary: data.answer || ""}));
        setState("$:/state/ai-gateway/summarizing", "");
        dbg("summarize ok; length=" + ((data.answer||"").length));
      }).catch(function(err) {
        setState("$:/state/ai-gateway/summarizing", "");
        setState("$:/state/ai-gateway/summary-error", askErrorMessage(err));
        dbg("summarize FAILED: " + err.message);
      });
    });

    dbg("ready - gateway=" + baseURL + " notebook=" + notebook + " apiKey=" + (apiKey ? "set(" + apiKey.length + ")" : "MISSING"));
  } catch (e) {
    dbg("STARTUP ERROR: " + (e && e.stack ? e.stack : e));
  }
};

})();
