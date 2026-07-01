#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const PLUGIN_ROOT = path.join(__dirname, "..", "plugins", "mblackman", "ai-gateway");
const BUILD_DIR = path.join(__dirname, "..", "build");

const pluginInfo = JSON.parse(
    fs.readFileSync(path.join(PLUGIN_ROOT, "plugin.info"), "utf8")
);
const twFiles = JSON.parse(
    fs.readFileSync(path.join(PLUGIN_ROOT, "tiddlywiki.files"), "utf8")
);

const tiddlers = {};

for (const entry of twFiles.tiddlers) {
    const filePath = path.join(PLUGIN_ROOT, entry.file);
    const text = fs.readFileSync(filePath, "utf8");
    tiddlers[entry.fields.title] = { ...entry.fields, text };
}

const pluginBody = JSON.stringify({ tiddlers }, null, 2);

// Write as a TiddlyWiki .tid file: field headers + blank line + JSON body.
// This format can be dragged into any TiddlyWiki to install the plugin.
const SKIP = new Set(["type", "text"]);
const header = Object.entries(pluginInfo)
    .filter(([k]) => !SKIP.has(k))
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");

const tid = `${header}\ntype: application/json\n\n${pluginBody}\n`;

fs.mkdirSync(BUILD_DIR, { recursive: true });
const outPath = path.join(BUILD_DIR, "ai-gateway.tid");
fs.writeFileSync(outPath, tid, "utf8");
console.log(`Built ${outPath} (${Object.keys(tiddlers).length} tiddlers)`);
