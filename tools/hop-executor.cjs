"use strict";
/* hop-executor.cjs — the box-side consumer for the STOCK Grok Bot cloud host
 * bundle `sand-host` aea062b (25,634,503 bytes, md5 ba86c15a449c5daa2be9e9a0f68fa7a7;
 * previously 1bcef91 — same unique seam strings).
 *
 * Installed on the box as /home/box/sand-data/hop-executor.cjs. The patched
 * bundle requires it at the main-turn executor seam and at the memory /
 * self-summary / dreaming getExecutor sites:
 *
 *     const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();
 *
 * The module returns null when the conversation has no binding, so the host
 * keeps its normal Cursor path. When a binding exists, the returned object
 * replaces the proto executor and talks to a local OpenAI-compatible shim.
 *
 * CONTRACT — BasePromptExecutor (bundle @15991774):
 *     appendMessages(messages) -> this
 *     getState()
 *     getMessages()
 *     clearMessages()
 *     stream(ctx, invocationId, tools, options)
 *       -> { fullStream, usage, extendedUsage, providerMetadata, invocationId, response }
 *
 * MESSAGES AT THIS SEAM ARE PLAIN CORE MESSAGES. They are NOT redacted
 * wrappers, so this module never unwraps anything: it converts what it stores
 * straight to chat/completions, exactly as the stock ProtoPromptExecutor feeds
 * this.builder.getMessages() straight into coreMessageToProto @23011518. See
 * the 19539275 and 16083633 notes below for the evidence.
 *
 * CONTRACT — createHopExecutor(host, session, deps):
 *     host     an object with getConversationId(), OR a non-empty
 *              conversation-id string (memory-dreaming seams pass agentId)
 *     session  the stock session; only used by the caller for the fallback
 *              (may be null at the dreaming sites)
 *     deps     OPTIONAL bag, { log? }. `log` receives one preformatted line
 *              instead of stderr. undefined, {} and { log } all behave the
 *              same; there is no required dependency, so the injected helper
 *              passes {} and closes over nothing.
 *
 * BUNDLE BYTE OFFSETS THIS FILE WAS DERIVED FROM (all re-read, not guessed):
 *   15991774  BasePromptBuilder / BasePromptExecutor / BaseMiddleware.
 *             getState() and getMessages() both return a COPY of the array.
 *             getExecutor() at the seam takes no state, so the array starts
 *             empty and the runner fills it with appendMessages().
 *   23046581  ProtoPromptSession.getExecutor -> new ProtoPromptBuilder(state).
 *   23035605  ProtoPromptExecutor.stream: builds the five side promises, each
 *             with preventUnhandledRejection, and returns the six values.
 *             createFullStream is an ASYNC GENERATOR, so fullStream is an
 *             async iterable, not a ReadableStream.
 *   23041637  protoResponseToStreamParts: the authoritative stream-part union.
 *   23011518  coreMessageToProto: the exact CoreMessage role and part shapes,
 *             and the proof they are PLAIN — it branches on
 *             `typeof msg.content === "string"` and `Array.isArray(msg.content)`.
 *             Roles and parts it accepts: system (string), user (string |
 *             text/image/file parts), assistant (string | text/tool-call/
 *             reasoning/redacted-reasoning parts), tool (tool-result parts).
 *             userContentPartToProto @23018305 is the per-part detail; an
 *             unknown user part becomes empty text there, which is what
 *             dropping it here amounts to.
 *   19539275  RedactedPromptToolExecutor. It WRAPS an inner executor — the slot
 *             this module fills. appendMessages() calls
 *               fromRedactedCoreMessages(arr, PrivacyCapability.UNSAFE_ALWAYS_ALLOWED)
 *             and forwards the PLAIN result to innerToolExecutor.appendMessages;
 *             getState()/getMessages() re-wrap on the way OUT with
 *             toRedactedCoreMessages; stream() forwards untouched. Unwrapping
 *             therefore happens OUTSIDE the inner executor, in both directions.
 *   19795174  the runner call site:
 *               fromRedactedCoreMessages(rootPromptExecutor.getMessages(),
 *                                        PrivacyCapability.UNSAFE_ALWAYS_ALLOWED)
 *             It operates on the OUTER redacted wrapper, never on the inner
 *             executor's own storage. (The analysis report cites 19794812, the
 *             enclosing step-setup block; the call itself starts at 19795174.)
 *   16083633  fromRedactedSystemMessage, body `message.content.unwrap(purpose,
 *             opts)`. An earlier version of this file unwrapped its own stored
 *             messages, which fed a PLAIN system message into this function and
 *             produced the live failure
 *               error=message.content.unwrap is not a function
 *             on a real box turn. That premise was wrong; plain in, plain out.
 *   19515664  streamModelAndCollectToolCalls: the stream() call site and the
 *             consumer of every part. It iterates fullStream with `for await`.
 *   19530567  SimplePromptToolExecutor / executeToolStream.
 *   24659906  sanitizeUsage / sanitizeExtendedUsage / sanitizeFullStream.
 *   24663896  UsageSanitizingMiddleware + sanitizeStreamResult. It reads
 *             response.modelId.trim(), so modelId must be a non-empty string
 *             for the resolved-model tracker to see the routed model.
 *
 * TOOL-CALL ORDERING, learned from the consumer at 19515664: the consumer keeps
 * ONE open tool-call stream and closes it as soon as a different toolCallId
 * arrives; a closed id is then dropped as a duplicate. So parts are emitted
 * GROUPED per tool call — start, deltas, tool-call — and the previous tool call
 * is completed before the next one starts. A provider that interleaves argument
 * fragments of two indices is not supported by the host either.
 *
 * BINDINGS FILE (canonical repo format, see examples/model-bindings.example.json):
 *     { "agents": { "<agentUuid>": { "modelId", "hopBaseUrl", ... } } }
 * A bare top-level map of uuid -> entry is also accepted. The file is read and
 * parsed on EVERY call, so an edit applies without a host restart.
 *
 * No dependencies: node builtins and global fetch only. Node 20.
 */

const fs = require("node:fs");
const crypto = require("node:crypto");

const DEFAULT_BINDINGS_PATH = "/home/box/sand-data/model-bindings.json";
const MAX_ERROR_BODY_CHARS = 400;

/* One stderr line per distinct problem, never one per call. */
let lastReportedProblem = "";

function writeLine(deps, text) {
  const line = "[opengrok] " + text + "\n";
  const log = deps && typeof deps.log === "function" ? deps.log : null;
  if (log) {
    try {
      log(line);
      return;
    } catch (_error) {
      /* fall through to stderr */
    }
  }
  try {
    process.stderr.write(line);
  } catch (_error) {
    /* stderr is gone; nothing else to do */
  }
}

function reportProblemOnce(deps, text) {
  if (lastReportedProblem === text) {
    return;
  }
  lastReportedProblem = text;
  writeLine(deps, text);
}

function bindingsPath() {
  const fromEnv = process.env.SAND_HOP_BINDINGS;
  return typeof fromEnv === "string" && fromEnv.trim() !== "" ? fromEnv : DEFAULT_BINDINGS_PATH;
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

function describeError(error) {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function toError(value) {
  return value instanceof Error ? value : new Error(String(value));
}

/* Read the bindings file and return the entry for this conversation, or null.
 * A missing file is normal and stays silent. A malformed file is reported once. */
function readBinding(conversationId, deps) {
  const path = bindingsPath();
  let raw;
  try {
    raw = fs.readFileSync(path, "utf8");
  } catch (error) {
    if (error && error.code !== "ENOENT" && error.code !== "ENOTDIR") {
      reportProblemOnce(deps, "hop bindings unreadable at " + path + ": " + describeError(error));
    }
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    reportProblemOnce(deps, "hop bindings malformed at " + path + ": " + describeError(error));
    return null;
  }
  if (!isPlainObject(parsed)) {
    reportProblemOnce(deps, "hop bindings malformed at " + path + ": root is not an object");
    return null;
  }
  const agents = isPlainObject(parsed.agents) ? parsed.agents : parsed;
  const entry = agents[conversationId];
  if (!isPlainObject(entry)) {
    return null;
  }
  if (!nonEmptyString(entry.modelId) || !nonEmptyString(entry.hopBaseUrl)) {
    reportProblemOnce(
      deps,
      "hop binding for " + conversationId + " is incomplete: modelId and hopBaseUrl are both required"
    );
    return null;
  }
  return {
    modelId: entry.modelId.trim(),
    hopBaseUrl: entry.hopBaseUrl.trim().replace(/\/+$/, "")
  };
}

/* A promise plus its settle functions, settled at most once. The host attaches
 * preventUnhandledRejection to all five; a rejection that nobody awaits must
 * never crash the host, so the same no-op handler is attached here. */
function createDeferred() {
  const box = { settled: false };
  box.promise = new Promise((resolve, reject) => {
    box.resolveFn = resolve;
    box.rejectFn = reject;
  });
  void box.promise.catch(() => {});
  box.resolve = (value) => {
    if (box.settled) {
      return;
    }
    box.settled = true;
    box.resolveFn(value);
  };
  box.reject = (error) => {
    if (box.settled) {
      return;
    }
    box.settled = true;
    box.rejectFn(error);
  };
  return box;
}

/* ---------------------------------------------------------------------------
 * PLAIN core message -> OpenAI chat/completions message conversion.
 * Part shapes come from coreMessageToProto @23011518 and
 * userContentPartToProto @23018305. Nothing here unwraps a redacted wrapper:
 * see the 19539275 note in the header for why none ever reaches this module.
 * ------------------------------------------------------------------------- */

function textFromParts(parts) {
  const chunks = [];
  for (const part of parts) {
    if (isPlainObject(part) && part.type === "text" && typeof part.text === "string") {
      chunks.push(part.text);
    }
  }
  return chunks.join("");
}

function imageDataUrl(part) {
  const image = part.image;
  const mimeType = nonEmptyString(part.mimeType) ? part.mimeType : "image/png";
  if (typeof image === "string") {
    if (/^(data:|https?:)/i.test(image)) {
      return image;
    }
    return "data:" + mimeType + ";base64," + image;
  }
  if (typeof URL === "function" && image instanceof URL) {
    return image.toString();
  }
  if (image instanceof Uint8Array) {
    return "data:" + mimeType + ";base64," + Buffer.from(image).toString("base64");
  }
  if (image instanceof ArrayBuffer) {
    return "data:" + mimeType + ";base64," + Buffer.from(new Uint8Array(image)).toString("base64");
  }
  return null;
}

function userContent(content) {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  const parts = [];
  let hasImage = false;
  for (const part of content) {
    if (!isPlainObject(part)) {
      continue;
    }
    if (part.type === "text" && typeof part.text === "string") {
      parts.push({ type: "text", text: part.text });
    } else if (part.type === "image") {
      const url = imageDataUrl(part);
      if (url === null) {
        parts.push({ type: "text", text: "[image omitted: unsupported encoding]" });
      } else {
        hasImage = true;
        parts.push({ type: "image_url", image_url: { url } });
      }
    } else if (part.type === "file") {
      parts.push({ type: "text", text: "[File: " + (part.name || "unnamed") + "]" });
    }
  }
  if (!hasImage) {
    /* Text-only history stays a plain string: the widest compatibility. */
    return parts.map((part) => part.text).join("");
  }
  return parts;
}

function toolResultText(part) {
  const result = part.result;
  if (typeof result === "string") {
    return result;
  }
  if (result === undefined || result === null) {
    return "";
  }
  try {
    return JSON.stringify(result);
  } catch (_error) {
    return String(result);
  }
}

function stringifyToolArgs(args) {
  if (typeof args === "string") {
    return args;
  }
  try {
    return JSON.stringify(args === undefined ? {} : args);
  } catch (_error) {
    return "{}";
  }
}

/* Reasoning parts are dropped: chat/completions has no input field for them. */
function coreMessagesToOpenAi(messages) {
  const out = [];
  for (const message of messages) {
    if (!isPlainObject(message)) {
      continue;
    }
    if (message.role === "system") {
      const text = typeof message.content === "string"
        ? message.content
        : Array.isArray(message.content) ? textFromParts(message.content) : "";
      out.push({ role: "system", content: text });
      continue;
    }
    if (message.role === "user") {
      out.push({ role: "user", content: userContent(message.content) });
      continue;
    }
    if (message.role === "assistant") {
      if (typeof message.content === "string") {
        out.push({ role: "assistant", content: message.content });
        continue;
      }
      if (!Array.isArray(message.content)) {
        continue;
      }
      const text = textFromParts(message.content);
      const toolCalls = [];
      for (const part of message.content) {
        if (isPlainObject(part) && part.type === "tool-call") {
          toolCalls.push({
            id: part.toolCallId,
            type: "function",
            function: { name: part.toolName, arguments: stringifyToolArgs(part.args) }
          });
        }
      }
      const assistant = { role: "assistant", content: text };
      if (toolCalls.length > 0) {
        assistant.tool_calls = toolCalls;
      }
      out.push(assistant);
      continue;
    }
    if (message.role === "tool" && Array.isArray(message.content)) {
      for (const part of message.content) {
        if (isPlainObject(part) && part.type === "tool-result") {
          out.push({
            role: "tool",
            tool_call_id: part.toolCallId,
            content: toolResultText(part)
          });
        }
      }
    }
  }
  return out;
}

/* Provider-defined tools have no chat/completions representation; the host
 * separates them the same way at 23035605. */
function toolsToOpenAi(tools) {
  const out = [];
  for (const tool of Array.isArray(tools) ? tools : []) {
    if (!isPlainObject(tool) || tool.type === "provider-defined") {
      continue;
    }
    if (!nonEmptyString(tool.name)) {
      continue;
    }
    out.push({
      type: "function",
      function: {
        name: tool.name,
        description: typeof tool.description === "string" ? tool.description : "",
        parameters: isPlainObject(tool.parameters) ? tool.parameters : { type: "object", properties: {} }
      }
    });
  }
  return out;
}

/* ---------------------------------------------------------------------------
 * SSE reading.
 * ------------------------------------------------------------------------- */

function sseDataPayload(line) {
  if (!line.startsWith("data:")) {
    return null;
  }
  const payload = line.slice(5).trim();
  return payload === "" ? null : payload;
}

async function* readSseData(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      buffer += decoder.decode(chunk.value, { stream: true });
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        const line = buffer.slice(0, index).replace(/\r$/, "");
        buffer = buffer.slice(index + 1);
        const payload = sseDataPayload(line);
        if (payload === "[DONE]") {
          return;
        }
        if (payload !== null) {
          yield payload;
        }
        index = buffer.indexOf("\n");
      }
    }
    const tail = sseDataPayload(buffer.replace(/\r$/, ""));
    if (tail !== null && tail !== "[DONE]") {
      yield tail;
    }
  } finally {
    try {
      await reader.cancel();
    } catch (_error) {
      /* the stream is already finished */
    }
  }
}

function usageFromChunk(chunk) {
  const usage = chunk && chunk.usage;
  if (!isPlainObject(usage)) {
    return null;
  }
  const promptTokens = Number(usage.prompt_tokens) || 0;
  const completionTokens = Number(usage.completion_tokens) || 0;
  const totalTokens = Number(usage.total_tokens) || promptTokens + completionTokens;
  const details = isPlainObject(usage.prompt_tokens_details) ? usage.prompt_tokens_details : {};
  return {
    promptTokens,
    completionTokens,
    totalTokens,
    cacheReadTokens: Number(details.cached_tokens) || 0
  };
}

/* ---------------------------------------------------------------------------
 * The executor.
 * ------------------------------------------------------------------------- */

class HopPromptExecutor {
  constructor(conversationId, binding, deps) {
    this.conversationId = conversationId;
    this.binding = binding;
    this.deps = deps;
    /* getExecutor() at the seam passes no state, so the array starts empty and
     * the runner appends the system prompt and the history. Mirrors
     * BasePromptBuilder @15991774. */
    this.messages = [];
    this.toolCallCounter = 0;
  }

  appendMessages(messages) {
    const incoming = Array.isArray(messages) ? messages : [messages];
    this.messages.push(...incoming);
    return this;
  }

  getState() {
    return [...this.messages];
  }

  getMessages() {
    return [...this.messages];
  }

  clearMessages() {
    this.messages = [];
  }

  nextToolCallId(index) {
    this.toolCallCounter += 1;
    return "hop-tool-" + index + "-" + this.toolCallCounter;
  }

  stream(ctx, invocationId, tools, options) {
    const usage = createDeferred();
    const extendedUsage = createDeferred();
    const providerMetadata = createDeferred();
    const invocation = createDeferred();
    const response = createDeferred();
    const resolvers = { usage, extendedUsage, providerMetadata, invocation, response };
    const fullStream = this.runStream(ctx, invocationId, tools, options, resolvers);
    return {
      fullStream,
      usage: usage.promise,
      extendedUsage: extendedUsage.promise,
      providerMetadata: providerMetadata.promise,
      invocationId: invocation.promise,
      response: response.promise
    };
  }

  /* Every path settles all five promises BEFORE the last yield, so a consumer
   * that stops iterating can never leave one pending (report risk 1). */
  async *runStream(ctx, invocationId, tools, options, resolvers) {
    const url = this.binding.hopBaseUrl + "/chat/completions";
    let status = 0;
    let failure = null;
    let usageValue = null;
    try {
      /* The messages held here are PLAIN core messages, never redacted ones:
       * RedactedPromptToolExecutor @19539275 unwraps on the way IN and re-wraps
       * on the way OUT, OUTSIDE the inner executor this module replaces. So the
       * conversion is direct, exactly as coreMessageToProto @23011518 does it.
       * getMessages() already returns a fresh array. */
      const body = {
        model: this.binding.modelId,
        messages: coreMessagesToOpenAi(this.getMessages()),
        tools: toolsToOpenAi(tools),
        stream: true,
        stream_options: { include_usage: true }
      };
      if (options && typeof options.maxTokens === "number") {
        body.max_tokens = options.maxTokens;
      }
      const signal = ctx && ctx.signal ? ctx.signal : undefined;
      const httpResponse = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal
      });
      status = httpResponse.status;
      if (!httpResponse.ok) {
        let detail = "";
        try {
          detail = (await httpResponse.text()).slice(0, MAX_ERROR_BODY_CHARS);
        } catch (_error) {
          detail = "";
        }
        throw new Error("hop request failed: HTTP " + status + (detail === "" ? "" : " " + detail));
      }
      if (!httpResponse.body) {
        throw new Error("hop request failed: HTTP " + status + " with no response body");
      }

      let responseId = "";
      let responseModelId = "";
      let text = "";
      let reasoning = "";
      let active = null;
      const finishedCalls = [];

      const closeActiveToolCall = () => {
        if (active === null) {
          return null;
        }
        let args;
        try {
          args = JSON.parse(active.argsText);
        } catch (_error) {
          args = {};
        }
        if (!isPlainObject(args) && !Array.isArray(args)) {
          args = {};
        }
        const part = { type: "tool-call", toolCallId: active.id, toolName: active.name, args };
        finishedCalls.push({ toolCallId: active.id, toolName: active.name, args });
        active = null;
        return part;
      };

      for await (const payload of readSseData(httpResponse.body)) {
        let chunk;
        try {
          chunk = JSON.parse(payload);
        } catch (_error) {
          continue;
        }
        if (!isPlainObject(chunk)) {
          continue;
        }
        if (nonEmptyString(chunk.id)) {
          responseId = chunk.id;
        }
        if (nonEmptyString(chunk.model)) {
          responseModelId = chunk.model;
        }
        const chunkUsage = usageFromChunk(chunk);
        if (chunkUsage !== null) {
          usageValue = chunkUsage;
        }
        const choice = Array.isArray(chunk.choices) ? chunk.choices[0] : undefined;
        const delta = isPlainObject(choice) && isPlainObject(choice.delta) ? choice.delta : null;
        if (delta === null) {
          continue;
        }
        const reasoningDelta = typeof delta.reasoning_content === "string"
          ? delta.reasoning_content
          : typeof delta.reasoning === "string" ? delta.reasoning : "";
        if (reasoningDelta !== "") {
          reasoning += reasoningDelta;
          yield { type: "reasoning", textDelta: reasoningDelta };
        }
        if (typeof delta.content === "string" && delta.content !== "") {
          text += delta.content;
          yield { type: "text-delta", textDelta: delta.content };
        }
        if (!Array.isArray(delta.tool_calls)) {
          continue;
        }
        for (const entry of delta.tool_calls) {
          if (!isPlainObject(entry)) {
            continue;
          }
          const index = typeof entry.index === "number" ? entry.index : 0;
          const fn = isPlainObject(entry.function) ? entry.function : {};
          if (active !== null && active.index !== index) {
            const completed = closeActiveToolCall();
            if (completed !== null) {
              yield completed;
            }
          }
          if (active === null) {
            active = {
              index,
              id: nonEmptyString(entry.id) ? entry.id : this.nextToolCallId(index),
              name: nonEmptyString(fn.name) ? fn.name : "",
              argsText: ""
            };
            yield { type: "tool-call-streaming-start", toolCallId: active.id, toolName: active.name };
          } else if (active.name === "" && nonEmptyString(fn.name)) {
            /* A provider that sends the name late. The consumer at 19515664
             * fills an empty toolName from a second start part. */
            active.name = fn.name;
            yield { type: "tool-call-streaming-start", toolCallId: active.id, toolName: active.name };
          }
          const fragment = typeof fn.arguments === "string" ? fn.arguments : "";
          if (fragment !== "") {
            active.argsText += fragment;
            yield {
              type: "tool-call-delta",
              toolCallId: active.id,
              toolName: "",
              argsTextDelta: fragment
            };
          }
        }
      }

      const lastCall = closeActiveToolCall();
      if (lastCall !== null) {
        yield lastCall;
      }

      const finalUsage = {
        promptTokens: usageValue === null ? 0 : usageValue.promptTokens,
        completionTokens: usageValue === null ? 0 : usageValue.completionTokens,
        totalTokens: usageValue === null ? 0 : usageValue.totalTokens
      };
      /* Content order mirrors protoMessagesToResponseMessages @23035605:
       * reasoning parts, then text, then tool calls. */
      const content = [];
      if (reasoning !== "") {
        content.push({ type: "reasoning", text: reasoning });
      }
      if (text !== "") {
        content.push({ type: "text", text });
      }
      for (const call of finishedCalls) {
        content.push({
          type: "tool-call",
          toolCallId: call.toolCallId,
          toolName: call.toolName,
          args: call.args
        });
      }
      resolvers.usage.resolve(finalUsage);
      resolvers.extendedUsage.resolve({
        inputTokens: finalUsage.promptTokens,
        outputTokens: finalUsage.completionTokens,
        cacheReadTokens: usageValue === null ? 0 : usageValue.cacheReadTokens,
        cacheWriteTokens: 0,
        maxTokens: 0
      });
      resolvers.providerMetadata.resolve(undefined);
      resolvers.invocation.resolve(nonEmptyString(invocationId) ? invocationId : crypto.randomUUID());
      resolvers.response.resolve({
        id: responseId,
        modelId: responseModelId === "" ? this.binding.modelId : responseModelId,
        timestamp: new Date(),
        messages: [{ id: responseId, role: "assistant", content }],
        supportsSelfSummary: false,
        earlyCompactionContextTokenThreshold: undefined
      });
      yield { type: "finish", finishReason: "stop", usage: finalUsage };
    } catch (error) {
      failure = toError(error);
      resolvers.usage.reject(failure);
      resolvers.extendedUsage.reject(failure);
      resolvers.providerMetadata.reject(failure);
      resolvers.invocation.reject(failure);
      resolvers.response.reject(failure);
      yield { type: "error", error: failure };
    } finally {
      /* Safety net: an abandoned generator still settles every promise. */
      const abandoned = new Error("hop stream ended before it settled");
      resolvers.usage.reject(abandoned);
      resolvers.extendedUsage.reject(abandoned);
      resolvers.providerMetadata.reject(abandoned);
      resolvers.invocation.reject(abandoned);
      resolvers.response.reject(abandoned);
      writeLine(
        this.deps,
        "hop conversation=" + this.conversationId +
          " model=" + this.binding.modelId +
          " url=" + url +
          " status=" + status +
          " prompt=" + (usageValue === null ? 0 : usageValue.promptTokens) +
          " completion=" + (usageValue === null ? 0 : usageValue.completionTokens) +
          " total=" + (usageValue === null ? 0 : usageValue.totalTokens) +
          (failure === null ? "" : " error=" + failure.message)
      );
    }
  }
}

/* Return an executor for this conversation, or null to keep the stock path. */
function createHopExecutor(host, session, deps) {
  let conversationId = null;
  if (typeof host === "string") {
    conversationId = host;
  } else if (host && typeof host.getConversationId === "function") {
    try {
      conversationId = host.getConversationId();
    } catch (error) {
      reportProblemOnce(deps, "hop executor disabled: getConversationId failed: " + describeError(error));
      return null;
    }
  } else {
    return null;
  }
  if (!nonEmptyString(conversationId)) {
    return null;
  }
  const binding = readBinding(conversationId, deps);
  if (binding === null) {
    return null;
  }
  if (typeof fetch !== "function") {
    reportProblemOnce(deps, "hop executor disabled: global fetch is missing");
    return null;
  }
  return new HopPromptExecutor(conversationId, binding, deps);
}

module.exports = { createHopExecutor };
