(function () {
  window.__gw = {
    ready() {
      // The wiki + sync layer have booted. Does NOT require user tiddlers,
      // so an empty notebook still counts as ready.
      return typeof $tw !== "undefined" && !!$tw.wiki && !!$tw.syncer;
    },

    filter(filterStr) {
      return $tw.wiki.filterTiddlers(filterStr);
    },

    filterFull(filterStr) {
      const titles = $tw.wiki.filterTiddlers(filterStr);
      return titles.map((title) => {
        const t = $tw.wiki.getTiddler(title);
        if (!t) return { title, fields: {}, text: "" };
        const fields = Object.assign({}, t.fields);
        const text = fields.text || "";
        delete fields.text;
        return { title, fields, text };
      });
    },

    getTiddler(title) {
      const t = $tw.wiki.getTiddler(title);
      if (!t) return null;
      const fields = Object.assign({}, t.fields);
      const text = fields.text || "";
      delete fields.text;
      return { title, fields, text };
    },

    putTiddler(title, fields, text) {
      $tw.wiki.addTiddler(new $tw.Tiddler(Object.assign({}, fields, { title, text })));
      return true;
    },

    deleteTiddler(title) {
      $tw.wiki.deleteTiddler(title);
      return true;
    },

    render(title, mode) {
      const type = mode === "html" ? "text/html" : "text/plain";
      return $tw.wiki.renderTiddler(type, title) || "";
    },

    renderText(text, mode) {
      const type = mode === "html" ? "text/html" : "text/plain";
      return $tw.wiki.renderText(type, "text/vnd.tiddlywiki", text) || "";
    },

    sync() {
      if ($tw.syncer && $tw.syncer.syncFromServer) {
        $tw.syncer.syncFromServer();
      }
      return true;
    },

    probe() {
      const allInputs = Array.from(document.querySelectorAll("input"));
      const inputs = allInputs.map((el, i) => {
        const rect = el.getBoundingClientRect();
        const visible =
          rect.width > 0 && rect.height > 0 && el.offsetParent !== null;
        // associated label text, if any
        let label = "";
        if (el.id) {
          const l = document.querySelector('label[for="' + el.id + '"]');
          if (l) label = l.textContent.trim();
        }
        if (!label && el.closest("label")) {
          label = el.closest("label").textContent.trim();
        }
        const parent = el.parentElement;
        // Nearby text — label cell in the same table row, or enclosing block.
        let context = "";
        const row = el.closest("tr") || el.closest("label") || parent;
        if (row) context = (row.textContent || "").trim().slice(0, 120);
        // Build a usable selector: prefer id/name, else a global nth-of-input
        let selector;
        if (el.id) selector = "#" + el.id;
        else if (el.name) selector = 'input[name="' + el.name + '"]';
        else selector = "input >> nth=" + i; // Playwright nth syntax
        return {
          index: i,
          type: el.type,
          name: el.name || "",
          id: el.id || "",
          class: el.className || "",
          placeholder: el.placeholder || "",
          ariaLabel: el.getAttribute("aria-label") || "",
          label: label,
          visible: visible,
          parent: parent
            ? parent.tagName.toLowerCase() +
              (parent.id ? "#" + parent.id : "") +
              (parent.className ? "." + String(parent.className).split(/\s+/).join(".") : "")
            : "",
          selector: selector,
          context: context,
          html: el.outerHTML.slice(0, 200),
        };
      });

      const twPresent = typeof $tw !== "undefined";
      let syncerMethods = [];
      let tiddlerCount = 0;
      let systemCount = 0;
      let hasTiddlyPwaFilter = false;

      if (twPresent && $tw.wiki) {
        tiddlerCount = $tw.wiki.filterTiddlers("[all[]]").length;
        systemCount = $tw.wiki.filterTiddlers("[is[system]]").length;
        // probe whether the tiddlypwa filter operator is registered
        try {
          $tw.wiki.filterTiddlers("[tiddlypwa[]]");
          hasTiddlyPwaFilter = true;
        } catch (_) {
          hasTiddlyPwaFilter = false;
        }
        if ($tw.syncer) {
          const proto = Object.getPrototypeOf($tw.syncer);
          syncerMethods = Object.getOwnPropertyNames(proto).filter(
            (k) => typeof proto[k] === "function" && k !== "constructor"
          );
        }
      }

      return { inputs, twPresent, syncerMethods, tiddlerCount, systemCount, hasTiddlyPwaFilter };
    },
  };
})();
