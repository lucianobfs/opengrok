"use strict";
/* test-hop-executor.cjs — contract tests for tools/hop-executor.cjs.
 *
 *     node tools/test-hop-executor.cjs
 *
 * No network beyond 127.0.0.1, no dependencies, no test framework. A local
 * http server replays canned OpenAI-style SSE turns. The tests drive the real
 * exported createHopExecutor; nothing under test is reimplemented here.
 *
 * The asserted stream-part sequences are the union from the report section 2
 * (protoResponseToStreamParts @23041637 in sand-host 1bcef91).
 */

const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const AGENT_ID = "00000000-0000-4000-8000-0000000000aa";
const OTHER_AGENT_ID = "00000000-0000-4000-8000-0000000000bb";

let passed = 0;
let failed = 0;
const pending = [];

function check(name, fn) {
  pending.push({ name, fn });
}

function eq(a, b, msg) {
  if (a !== b) {
    throw new Error((msg || "eq") + ": got " + JSON.stringify(a) + " want " + JSON.stringify(b));
  }
}

function deepEq(a, b, msg) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error((msg || "deepEq") + ": got " + JSON.stringify(a) + " want " + JSON.stringify(b));
  }
}

function ok(value, msg) {
  if (!value) {
    throw new Error(msg || "expected a truthy value");
  }
}

/* --- fixtures ------------------------------------------------------------ */

/* The runner unwraps redacted messages before the request. The fake dep is the
 * identity function, so the test drives plain core messages. */
const deps = {
  fromRedactedCoreMessages: (messages, capability) => {
    if (capability !== "UNSAFE_ALWAYS_ALLOWED") {
      throw new Error("wrong privacy capability: " + String(capability));
    }
    return messages;
  },
  PrivacyCapability: { UNSAFE_ALWAYS_ALLOWED: "UNSAFE_ALWAYS_ALLOWED" },
  log: () => {}
};

const host = { getConversationId: () => AGENT_ID };
const session = { getExecutor: () => { throw new Error("the stock executor must not be used"); } };

let tempDir = "";

function writeBindings(text) {
  const file = path.join(tempDir, "model-bindings.json");
  fs.writeFileSync(file, text, "utf8");
  process.env.SAND_HOP_BINDINGS = file;
  return file;
}

function bindingsFor(baseUrl) {
  return JSON.stringify({
    agents: {
      [AGENT_ID]: { name: "test", modelId: "test-model", hopBaseUrl: baseUrl, provider: "local" }
    }
  });
}

/* Serialize a list of chunk objects as an SSE body, closed with [DONE]. */
function sse(chunks) {
  return chunks.map((chunk) => "data: " + JSON.stringify(chunk) + "\n\n").join("") + "data: [DONE]\n\n";
}

/* --- the replay server --------------------------------------------------- */

const routes = new Map();
let lastRequestBody = null;
let server = null;
let baseUrl = "";

function startServer() {
  return new Promise((resolve) => {
    server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        try {
          lastRequestBody = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        } catch (_error) {
          lastRequestBody = null;
        }
        const route = routes.get(req.url) || (() => {
          res.writeHead(404, { "content-type": "text/plain" });
          res.end("no route");
        });
        route(req, res);
      });
    });
    server.listen(0, "127.0.0.1", () => {
      baseUrl = "http://127.0.0.1:" + server.address().port + "/v1";
      resolve();
    });
  });
}

function sseRoute(chunkList) {
  return (_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(sse(chunkList));
  };
}

/* --- helpers that drive the executor ------------------------------------- */

function makeExecutor() {
  const executor = require("./hop-executor.cjs").createHopExecutor(host, session, deps);
  ok(executor !== null, "expected an executor for the bound conversation");
  executor.appendMessages([
    { role: "system", content: "you are a test" },
    { role: "user", content: "hello" }
  ]);
  return executor;
}

async function drain(result) {
  const parts = [];
  for await (const part of result.fullStream) {
    parts.push(part);
  }
  return parts;
}

/* Report how each of the five side promises settled. Never leaves one pending:
 * a promise that is still pending after a macrotask turn is reported as such. */
async function settleReport(result) {
  const names = ["usage", "extendedUsage", "providerMetadata", "invocationId", "response"];
  const report = {};
  await Promise.all(names.map(async (name) => {
    const marker = {};
    const timer = new Promise((resolve) => setTimeout(() => resolve(marker), 50));
    const outcome = await Promise.race([
      result[name].then((value) => ({ state: "resolved", value }), (error) => ({ state: "rejected", error })),
      timer
    ]);
    report[name] = outcome === marker ? { state: "pending" } : outcome;
  }));
  return report;
}

function assertAllSettled(report, state) {
  for (const name of Object.keys(report)) {
    eq(report[name].state, state, "promise " + name + " must be " + state);
  }
}

/* --- cases --------------------------------------------------------------- */

check("null when the bindings file is missing", () => {
  process.env.SAND_HOP_BINDINGS = path.join(tempDir, "absent-bindings.json");
  eq(require("./hop-executor.cjs").createHopExecutor(host, session, deps), null);
});

check("null when the bindings file is malformed", () => {
  writeBindings("{ this is not json");
  eq(require("./hop-executor.cjs").createHopExecutor(host, session, deps), null);
});

check("null when no entry matches the conversation id", () => {
  writeBindings(JSON.stringify({
    agents: { [OTHER_AGENT_ID]: { modelId: "m", hopBaseUrl: "http://127.0.0.1:1/v1" } }
  }));
  eq(require("./hop-executor.cjs").createHopExecutor(host, session, deps), null);
});

check("null when the entry has no hopBaseUrl", () => {
  writeBindings(JSON.stringify({ agents: { [AGENT_ID]: { modelId: "m" } } }));
  eq(require("./hop-executor.cjs").createHopExecutor(host, session, deps), null);
});

check("a bare top-level map is accepted as a fallback shape", () => {
  writeBindings(JSON.stringify({ [AGENT_ID]: { modelId: "m", hopBaseUrl: "http://127.0.0.1:1/v1" } }));
  ok(require("./hop-executor.cjs").createHopExecutor(host, session, deps) !== null);
});

check("bindings are re-read on every call, so an edit needs no restart", () => {
  writeBindings(JSON.stringify({ agents: {} }));
  eq(require("./hop-executor.cjs").createHopExecutor(host, session, deps), null);
  writeBindings(bindingsFor("http://127.0.0.1:1/v1"));
  ok(require("./hop-executor.cjs").createHopExecutor(host, session, deps) !== null);
});

check("executor mirrors the BasePromptBuilder message semantics", () => {
  writeBindings(bindingsFor("http://127.0.0.1:1/v1"));
  const executor = require("./hop-executor.cjs").createHopExecutor(host, session, deps);
  deepEq(executor.getMessages(), []);
  const back = executor.appendMessages([{ role: "user", content: "a" }]);
  eq(back, executor, "appendMessages must return this");
  executor.appendMessages({ role: "user", content: "b" });
  eq(executor.getMessages().length, 2);
  deepEq(executor.getState(), executor.getMessages(), "getState must mirror getMessages");
  ok(executor.getMessages() !== executor.getMessages(), "getMessages must return a copy");
  executor.clearMessages();
  deepEq(executor.getMessages(), []);
});

check("text-only turn emits text deltas then finish with the final usage", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-1", model: "test-model", choices: [{ index: 0, delta: { role: "assistant" } }] },
    { id: "resp-1", model: "test-model", choices: [{ index: 0, delta: { content: "Hel" } }] },
    { id: "resp-1", model: "test-model", choices: [{ index: 0, delta: { content: "lo" } }] },
    { id: "resp-1", model: "test-model", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    { id: "resp-1", model: "test-model", choices: [], usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14 } }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({ signal: undefined }, "inv-1", [], {});
  const parts = await drain(result);
  deepEq(parts, [
    { type: "text-delta", textDelta: "Hel" },
    { type: "text-delta", textDelta: "lo" },
    { type: "finish", finishReason: "stop", usage: { promptTokens: 11, completionTokens: 3, totalTokens: 14 } }
  ]);
  const report = await settleReport(result);
  assertAllSettled(report, "resolved");
  deepEq(report.usage.value, { promptTokens: 11, completionTokens: 3, totalTokens: 14 });
  deepEq(report.extendedUsage.value, {
    inputTokens: 11, outputTokens: 3, cacheReadTokens: 0, cacheWriteTokens: 0, maxTokens: 0
  });
  eq(report.providerMetadata.value, undefined);
  eq(report.invocationId.value, "inv-1");
  const response = report.response.value;
  eq(response.id, "resp-1");
  eq(response.modelId, "test-model");
  ok(response.timestamp instanceof Date, "timestamp must be a Date");
  eq(response.supportsSelfSummary, false);
  eq(response.earlyCompactionContextTokenThreshold, undefined);
  deepEq(response.messages, [
    { id: "resp-1", role: "assistant", content: [{ type: "text", text: "Hello" }] }
  ]);
});

check("the request body carries the converted messages and tools", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-b", model: "test-model", choices: [{ index: 0, delta: { content: "ok" } }] }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = require("./hop-executor.cjs").createHopExecutor(host, session, deps);
  executor.appendMessages([
    { role: "system", content: "sys" },
    { role: "user", content: [{ type: "text", text: "look" }] },
    {
      role: "assistant",
      content: [
        { type: "reasoning", text: "dropped on the wire" },
        { type: "text", text: "calling" },
        { type: "tool-call", toolCallId: "call_x", toolName: "read", args: { p: "a" } }
      ]
    },
    {
      role: "tool",
      content: [{ type: "tool-result", toolCallId: "call_x", toolName: "read", result: { lines: 2 } }]
    }
  ]);
  await drain(executor.stream({}, "inv-b", [
    { name: "read", description: "read a file", parameters: { type: "object", properties: {} } },
    { type: "provider-defined", name: "web_search", description: "", parameters: {} }
  ], { maxTokens: 512 }));
  eq(lastRequestBody.model, "test-model");
  eq(lastRequestBody.stream, true);
  deepEq(lastRequestBody.stream_options, { include_usage: true });
  eq(lastRequestBody.max_tokens, 512);
  deepEq(lastRequestBody.messages, [
    { role: "system", content: "sys" },
    { role: "user", content: "look" },
    {
      role: "assistant",
      content: "calling",
      tool_calls: [{ id: "call_x", type: "function", function: { name: "read", arguments: "{\"p\":\"a\"}" } }]
    },
    { role: "tool", tool_call_id: "call_x", content: "{\"lines\":2}" }
  ]);
  deepEq(lastRequestBody.tools, [
    {
      type: "function",
      function: { name: "read", description: "read a file", parameters: { type: "object", properties: {} } }
    }
  ], "provider-defined tools must be skipped");
});

check("an image part becomes an image_url data url", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-i", model: "test-model", choices: [{ index: 0, delta: { content: "ok" } }] }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = require("./hop-executor.cjs").createHopExecutor(host, session, deps);
  executor.appendMessages([{
    role: "user",
    content: [
      { type: "text", text: "see" },
      { type: "image", image: new Uint8Array([1, 2, 3]), mimeType: "image/png" }
    ]
  }]);
  await drain(executor.stream({}, "inv-i", [], {}));
  deepEq(lastRequestBody.messages, [{
    role: "user",
    content: [
      { type: "text", text: "see" },
      { type: "image_url", image_url: { url: "data:image/png;base64," + Buffer.from([1, 2, 3]).toString("base64") } }
    ]
  }]);
});

check("two parallel tool calls stream as grouped fragments", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { role: "assistant" } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 0, id: "call_a", type: "function", function: { name: "read", arguments: "" } }
    ] } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 0, function: { arguments: "{\"path\":" } }
    ] } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 0, function: { arguments: "\"a.txt\"}" } }
    ] } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 1, id: "call_b", type: "function", function: { name: "write", arguments: "{\"n\":" } }
    ] } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 1, function: { arguments: "1}" } }
    ] } }] },
    { id: "resp-2", model: "test-model", choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] },
    { id: "resp-2", model: "test-model", choices: [], usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 } }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({}, "inv-2", [], {});
  const parts = await drain(result);
  deepEq(parts, [
    { type: "tool-call-streaming-start", toolCallId: "call_a", toolName: "read" },
    { type: "tool-call-delta", toolCallId: "call_a", toolName: "", argsTextDelta: "{\"path\":" },
    { type: "tool-call-delta", toolCallId: "call_a", toolName: "", argsTextDelta: "\"a.txt\"}" },
    { type: "tool-call", toolCallId: "call_a", toolName: "read", args: { path: "a.txt" } },
    { type: "tool-call-streaming-start", toolCallId: "call_b", toolName: "write" },
    { type: "tool-call-delta", toolCallId: "call_b", toolName: "", argsTextDelta: "{\"n\":" },
    { type: "tool-call-delta", toolCallId: "call_b", toolName: "", argsTextDelta: "1}" },
    { type: "tool-call", toolCallId: "call_b", toolName: "write", args: { n: 1 } },
    { type: "finish", finishReason: "stop", usage: { promptTokens: 20, completionTokens: 8, totalTokens: 28 } }
  ]);
  const report = await settleReport(result);
  assertAllSettled(report, "resolved");
  deepEq(report.response.value.messages, [{
    id: "resp-2",
    role: "assistant",
    content: [
      { type: "tool-call", toolCallId: "call_a", toolName: "read", args: { path: "a.txt" } },
      { type: "tool-call", toolCallId: "call_b", toolName: "write", args: { n: 1 } }
    ]
  }]);
});

check("unparsable tool arguments fall back to an empty object", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-3", model: "test-model", choices: [{ index: 0, delta: { tool_calls: [
      { index: 0, id: "call_c", function: { name: "read", arguments: "{not json" } }
    ] } }] }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const parts = await drain(executor.stream({}, "inv-3", [], {}));
  deepEq(parts, [
    { type: "tool-call-streaming-start", toolCallId: "call_c", toolName: "read" },
    { type: "tool-call-delta", toolCallId: "call_c", toolName: "", argsTextDelta: "{not json" },
    { type: "tool-call", toolCallId: "call_c", toolName: "read", args: {} },
    { type: "finish", finishReason: "stop", usage: { promptTokens: 0, completionTokens: 0, totalTokens: 0 } }
  ], "usage is zeroed when the provider sends none");
});

check("reasoning_content becomes reasoning parts before the text", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-4", model: "test-model", choices: [{ index: 0, delta: { reasoning_content: "step " } }] },
    { id: "resp-4", model: "test-model", choices: [{ index: 0, delta: { reasoning_content: "one" } }] },
    { id: "resp-4", model: "test-model", choices: [{ index: 0, delta: { content: "done" } }] },
    { id: "resp-4", model: "test-model", choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 } }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({}, "inv-4", [], {});
  const parts = await drain(result);
  deepEq(parts, [
    { type: "reasoning", textDelta: "step " },
    { type: "reasoning", textDelta: "one" },
    { type: "text-delta", textDelta: "done" },
    { type: "finish", finishReason: "stop", usage: { promptTokens: 5, completionTokens: 2, totalTokens: 7 } }
  ]);
  const report = await settleReport(result);
  deepEq(report.response.value.messages, [{
    id: "resp-4",
    role: "assistant",
    content: [
      { type: "reasoning", text: "step one" },
      { type: "text", text: "done" }
    ]
  }]);
});

check("the legacy `reasoning` delta field is accepted too", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-5", model: "test-model", choices: [{ index: 0, delta: { reasoning: "hmm" } }] }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const parts = await drain(executor.stream({}, "inv-5", [], {}));
  deepEq(parts[0], { type: "reasoning", textDelta: "hmm" });
});

check("http 500 emits one error part and rejects all five promises", async () => {
  routes.set("/v1/chat/completions", (_req, res) => {
    res.writeHead(500, { "content-type": "text/plain" });
    res.end("upstream exploded");
  });
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({}, "inv-6", [], {});
  const parts = await drain(result);
  eq(parts.length, 1, "only the error part is emitted");
  eq(parts[0].type, "error");
  ok(parts[0].error instanceof Error, "the error part carries an Error");
  ok(parts[0].error.message.indexOf("HTTP 500") >= 0, "the message names the status");
  ok(parts[0].error.message.indexOf("upstream exploded") >= 0, "the message carries the body");
  const report = await settleReport(result);
  assertAllSettled(report, "rejected");
});

check("a connection refusal rejects all five promises", async () => {
  writeBindings(bindingsFor("http://127.0.0.1:1/v1"));
  const executor = makeExecutor();
  const result = executor.stream({}, "inv-7", [], {});
  const parts = await drain(result);
  eq(parts.length, 1);
  eq(parts[0].type, "error");
  const report = await settleReport(result);
  assertAllSettled(report, "rejected");
});

check("an aborted signal emits an error part and rejects all five promises", async () => {
  const controller = new AbortController();
  routes.set("/v1/chat/completions", (_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: " + JSON.stringify({
      id: "resp-8", model: "test-model", choices: [{ index: 0, delta: { content: "start" } }]
    }) + "\n\n");
    /* The response never finishes; the abort is the only way out. */
    setTimeout(() => controller.abort(), 20);
  });
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({ signal: controller.signal }, "inv-8", [], {});
  const parts = await drain(result);
  eq(parts[0].type, "text-delta", "the delta seen before the abort still arrives");
  eq(parts[parts.length - 1].type, "error", "the abort surfaces as an error part");
  const report = await settleReport(result);
  assertAllSettled(report, "rejected");
});

check("an abandoned stream still settles every promise", async () => {
  routes.set("/v1/chat/completions", sseRoute([
    { id: "resp-9", model: "test-model", choices: [{ index: 0, delta: { content: "a" } }] },
    { id: "resp-9", model: "test-model", choices: [{ index: 0, delta: { content: "b" } }] }
  ]));
  writeBindings(bindingsFor(baseUrl));
  const executor = makeExecutor();
  const result = executor.stream({}, "inv-9", [], {});
  const iterator = result.fullStream[Symbol.asyncIterator]();
  const first = await iterator.next();
  eq(first.value.type, "text-delta");
  await iterator.return(undefined);
  const report = await settleReport(result);
  assertAllSettled(report, "rejected");
});

/* --- runner -------------------------------------------------------------- */

async function main() {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hop-executor-test-"));
  await startServer();
  for (const item of pending) {
    try {
      await item.fn();
      passed += 1;
      console.log("PASS " + item.name);
    } catch (error) {
      failed += 1;
      console.log("FAIL " + item.name + " :: " + (error && error.message));
    }
  }
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(tempDir, { recursive: true, force: true });
  console.log("");
  console.log(passed + "/" + (passed + failed) + " hop-executor pass, " + failed + " fail");
  process.exit(failed ? 1 : 0);
}

main().catch((error) => {
  console.log("FAIL harness :: " + (error && error.stack));
  process.exit(1);
});
